#!/usr/bin/env bash
# Nightly Immich backup mirror: n5 (TrueNAS, primary) -> vv (Synology, cold backup).
#
# Implements Phase 3 of raw/PRIMARY-SWAP.md. vv is a pure file+dump backup (its
# Immich stack is down); vv<->n5 (~600 km) is the offsite leg of 3-2-1. A naive
# rsync is not enough, so this job also:
#   - self-heals the recurring perm disease on originals (faeton:faeton 755/644),
#     so a faeton-run mirror never silently skips files Immich left root-locked;
#   - takes a logical pg_dump FIRST (a DB referencing missing files is worse than
#     extra unreferenced files), with a self-describing restore recipe;
#   - mirrors from an atomic ZFS snapshot view, not the live tree;
#   - also covers immy's own irreplaceable state: /mnt/flash/immy/{state,sidecars}
#     (mirrored from a transient snapshot of the `flash` pool dataset) and the
#     dedup manifest.sqlite (a live SQLite DB: copied with `sqlite3 .backup`
#     into a staging dir, integrity-checked, then rsynced — never raw);
#   - reports to a Healthchecks dead-man's-switch so a *missed* run alerts too.
#
# Runs as `faeton` (passwordless sudo); uses sudo only for zfs/docker/chown and
# pushes to vv over faeton's existing ssh key. See README.md.
#
# Order is load-bearing: perm-fix -> dump + manifest copy (local) ->
# snapshot+files->vv -> dump + manifest->vv.
# Files reach vv before the dump that references them.
#
# Usage:
#   DRY_RUN=1 ./nightly-mirror.sh     # rsync --dry-run, no deletes/writes to vv
#   ./nightly-mirror.sh               # live (what cron runs)

set -euo pipefail

# --- config -----------------------------------------------------------------
# mirror.env (next to this script, or $MIRROR_ENV) overrides any default below.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIRROR_ENV="${MIRROR_ENV:-$SELF_DIR/mirror.env}"
# A DRY_RUN passed on the command line / environment is the safety toggle and
# MUST win over whatever mirror.env says — capture it before sourcing.
_CLI_DRY_RUN="${DRY_RUN:-}"
# shellcheck source=/dev/null
[ -f "$MIRROR_ENV" ] && . "$MIRROR_ENV"
[ -n "$_CLI_DRY_RUN" ] && DRY_RUN="$_CLI_DRY_RUN"

HC_URL="${HC_URL:-}"                                   # Healthchecks ping URL (empty = no pings)
SSH_HOST="${SSH_HOST:-vv}"                             # ssh alias or host for the Synology
SSH_PORT="${SSH_PORT:-2033}"
SSH_USER="${SSH_USER:-faeton}"
SSH_KEY="${SSH_KEY:-}"                                 # optional explicit identity file
VV_ROOT="${VV_ROOT:-/volume1/faeton-immi}"            # backup tree root on vv

ORIGINALS="${ORIGINALS:-/mnt/tank/immich/originals}"   # external-library originals (we own these)
MEDIA="${MEDIA:-/mnt/tank/immich/media}"               # /data root: library/profile/upload/...
ORIGINALS_DS="${ORIGINALS_DS:-tank/immich/originals}"  # ZFS dataset names (for snapshots)
MEDIA_DS="${MEDIA_DS:-tank/immich/media}"
# immy state lives on the flash pool, in plain directories of the root `flash`
# dataset (verified: `findmnt -T /mnt/flash/immy/state` -> flash; no child
# dataset). We snapshot that dataset transiently like the tank ones and rsync
# from its .zfs view; FLASH_MOUNT is the dataset's mountpoint (paths under it
# map into the snapshot view).
FLASH_DS="${FLASH_DS:-flash}"
FLASH_MOUNT="${FLASH_MOUNT:-/mnt/flash}"
IMMY_STATE="${IMMY_STATE:-/mnt/flash/immy/state}"        # per-trip state (.immy markers etc.)
IMMY_SIDECARS="${IMMY_SIDECARS:-/mnt/flash/immy/sidecars}"
# Dedup manifest: a live SQLite DB (WAL) on tank (root dataset, root-owned).
MANIFEST_DB="${MANIFEST_DB:-/mnt/tank/media/state/manifest.sqlite}"
MANIFEST_STAGE="${MANIFEST_STAGE:-/mnt/tank/immich/backups/nightly/manifest-stage}"
DUMP_DIR="${DUMP_DIR:-/mnt/tank/immich/backups/nightly}"  # faeton-owned; created if missing
PG_CONTAINER="${PG_CONTAINER:-immich_postgres}"
OWNER="${OWNER:-faeton:faeton}"                        # perm-normalize target owner

KEEP_DUMPS="${KEEP_DUMPS:-14}"
MAX_DELETE="${MAX_DELETE:-200}"                        # fat-finger guard on rsync --delete
MIN_DUMP_BYTES="${MIN_DUMP_BYTES:-1000000}"           # sanity floor (~1 MB) for a real dump
DRY_RUN="${DRY_RUN:-0}"

LOCKFILE="${LOCKFILE:-$SELF_DIR/.mirror.lock}"   # beside the script (install dir must be faeton-writable)
LOG_DIR="${LOG_DIR:-$SELF_DIR/logs}"
KEEP_LOG_DAYS="${KEEP_LOG_DAYS:-30}"

# --- single-instance lock (no overlapping runs) -----------------------------
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  echo "another mirror run holds $LOCKFILE; exiting." >&2
  exit 0
fi

# --- logging ----------------------------------------------------------------
mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/mirror-$TS.log"
exec > >(tee -a "$LOG") 2>&1
log() { echo "[$(date +%H:%M:%S)] $*"; }

# --- Healthchecks heartbeat -------------------------------------------------
hc() {  # hc <path-suffix> [extra curl args...]
  [ -n "$HC_URL" ] || return 0
  curl -fsS -m 10 --retry 3 "${HC_URL}${1}" "${@:2}" >/dev/null 2>&1 || true
}

# --- snapshots are created mid-run; always tear them down on exit -----------
SNAP="mirror-$TS"
cleanup_snapshots() {
  for ds in "$ORIGINALS_DS" "$MEDIA_DS" "$FLASH_DS"; do
    if sudo zfs list -H -t snapshot "$ds@$SNAP" >/dev/null 2>&1; then
      sudo zfs destroy "$ds@$SNAP" 2>/dev/null || log "WARN: could not destroy $ds@$SNAP"
    fi
  done
}

on_exit() {
  local rc=$?
  cleanup_snapshots
  if [ "$rc" -eq 0 ]; then
    log "SUCCESS"
    hc ""
  else
    log "FAILED (exit $rc)"
    hc /fail --data-raw "$(tail -c 5000 "$LOG")"
  fi
  # prune old logs
  find "$LOG_DIR" -name 'mirror-*.log' -type f -mtime "+$KEEP_LOG_DAYS" -delete 2>/dev/null || true
}
trap on_exit EXIT

hc /start
log "mirror start (DRY_RUN=$DRY_RUN) -> ${SSH_USER}@${SSH_HOST}:${VV_ROOT}"

# ssh transport reused by every rsync below
SSH_CMD="ssh -p $SSH_PORT -o BatchMode=yes"
[ -n "$SSH_KEY" ] && SSH_CMD="$SSH_CMD -i $SSH_KEY"
REMOTE="${SSH_USER}@${SSH_HOST}"

# --- preflight (cheap checks before anything destructive/network) -----------
log "preflight..."
sudo zfs list -H "$ORIGINALS_DS" "$MEDIA_DS" "$FLASH_DS" >/dev/null \
  || { log "ERROR: datasets $ORIGINALS_DS / $MEDIA_DS / $FLASH_DS missing"; exit 1; }
command -v sqlite3 >/dev/null || { log "ERROR: sqlite3 not installed (needed for manifest .backup)"; exit 1; }
[ -f "$MANIFEST_DB" ] || { log "ERROR: manifest $MANIFEST_DB not found"; exit 1; }
for d in "$IMMY_STATE" "$IMMY_SIDECARS"; do
  case "$d" in "$FLASH_MOUNT"/*) ;; *) log "ERROR: $d is not under FLASH_MOUNT=$FLASH_MOUNT"; exit 1;; esac
  [ -d "$d" ] || { log "ERROR: $d missing"; exit 1; }
done
[ "$(sudo docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null)" = "true" ] \
  || { log "ERROR: container $PG_CONTAINER not running"; exit 1; }
# vv reachable and target dirs writable by the ssh user
$SSH_CMD "$REMOTE" "test -w '$VV_ROOT/originals' && test -w '$VV_ROOT/media' && test -w '$VV_ROOT/db'" \
  || { log "ERROR: vv unreachable or $VV_ROOT/{originals,media,db} not writable by $SSH_USER"; exit 1; }
sudo install -d -o "${OWNER%%:*}" -g "${OWNER##*:}" "$DUMP_DIR"
log "preflight OK"

# === 1. perm self-heal: n5 originals only (fix mismatches only) =============
log "perm-normalize $ORIGINALS -> $OWNER 755/644 ..."
n_own=$(sudo find "$ORIGINALS" ! -type l \( ! -user "${OWNER%%:*}" -o ! -group "${OWNER##*:}" \) -printf . 2>/dev/null | wc -c)
sudo find "$ORIGINALS" ! -type l \( ! -user "${OWNER%%:*}" -o ! -group "${OWNER##*:}" \) -exec chown "$OWNER" {} +
n_dir=$(sudo find "$ORIGINALS" -type d ! -perm 0755 -printf . 2>/dev/null | wc -c)
sudo find "$ORIGINALS" -type d ! -perm 0755 -exec chmod 0755 {} +
n_file=$(sudo find "$ORIGINALS" -type f ! -perm 0644 -printf . 2>/dev/null | wc -c)
sudo find "$ORIGINALS" -type f ! -perm 0644 -exec chmod 0644 {} +
log "perm-normalize: chowned $n_own, dirs->755 $n_dir, files->644 $n_file"

# === 2. pg_dump FIRST (local), verify, write recipe, prune ==================
DUMP="$DUMP_DIR/immich-$TS.sql.gz"
log "pg_dumpall -> $DUMP ..."
sudo docker exec "$PG_CONTAINER" pg_dumpall --clean --if-exists -U postgres | gzip -c > "$DUMP"
gzip -t "$DUMP" || { log "ERROR: dump failed gzip integrity test"; exit 1; }
dump_bytes=$(stat -c %s "$DUMP")
[ "$dump_bytes" -ge "$MIN_DUMP_BYTES" ] \
  || { log "ERROR: dump only $dump_bytes bytes (< $MIN_DUMP_BYTES); refusing"; exit 1; }
log "dump OK ($dump_bytes bytes)"

RECIPE="$DUMP_DIR/RESTORE-RECIPE.txt"
immich_img=$(sudo docker inspect -f '{{.Config.Image}}' immich_server 2>/dev/null || echo '?')
pg_img=$(sudo docker inspect -f '{{.Config.Image}}' "$PG_CONTAINER" 2>/dev/null || echo '?')
cat > "$RECIPE" <<EOF
Immich restore recipe — generated $TS by nightly-mirror.sh
=========================================================
immich-server image : $immich_img
postgres image      : $pg_img
container media root : /data   (host: $MEDIA)
external originals    : /mnt/external/originals  (host: $ORIGINALS)

Restore (on a host with the SAME image tags above):
  1. Deploy the compose stack pinned to the immich/postgres tags above; stack DOWN.
  2. Start ONLY postgres, then load the dump:
       gunzip -c immich-<ts>.sql.gz | docker exec -i $PG_CONTAINER psql -U postgres -d postgres
  3. Place files so container paths resolve:
       $VV_ROOT/media/library  -> /data/library      (uploaded-asset originals)
       $VV_ROOT/originals      -> /mnt/external/originals (external libraries, :ro)
     immy state (optional, not Immich): $VV_ROOT/immy/state     -> $IMMY_STATE
                                        $VV_ROOT/immy/sidecars  -> $IMMY_SIDECARS
                                        $VV_ROOT/immy/manifest/manifest.sqlite -> $MANIFEST_DB
       (thumbs/ + encoded-video/ are NOT backed up — they regenerate.)
  4. Start the full stack; run ANALYZE; if smart search misbehaves, REINDEX the
     VectorChord index. Run the Phase 2 acceptance checklist before trusting it.
EOF
log "recipe written: $RECIPE"

# prune local dumps (keep newest KEEP_DUMPS)
ls -1t "$DUMP_DIR"/immich-*.sql.gz 2>/dev/null | tail -n "+$((KEEP_DUMPS + 1))" | while read -r old; do
  log "prune local $old"; rm -f "$old"
done

# === 2b. manifest.sqlite: consistent copy via sqlite3 .backup ===============
# A live (WAL) SQLite file must never be copied raw. .backup takes a consistent
# image even while a writer is active; sudo because the DB + -shm are root-owned.
# The copy lands in a staging dir that step 4 rsyncs.
sudo install -d -o "${OWNER%%:*}" -g "${OWNER##*:}" "$MANIFEST_STAGE"
MANIFEST_COPY="$MANIFEST_STAGE/manifest.sqlite"
rm -f "$MANIFEST_COPY" "$MANIFEST_COPY.tmp"
log "sqlite3 .backup $MANIFEST_DB -> $MANIFEST_COPY ..."
sudo sqlite3 "$MANIFEST_DB" ".backup '$MANIFEST_COPY.tmp'"
sudo chown "$OWNER" "$MANIFEST_COPY.tmp"
[ "$(sqlite3 "$MANIFEST_COPY.tmp" 'PRAGMA integrity_check;')" = "ok" ] \
  || { log "ERROR: manifest backup failed integrity_check"; exit 1; }
mv -f "$MANIFEST_COPY.tmp" "$MANIFEST_COPY"
log "manifest backup OK ($(stat -c %s "$MANIFEST_COPY") bytes)"

# === 3. atomic snapshot + mirror files to vv ================================
log "zfs snapshot @$SNAP ..."
sudo zfs snapshot "$ORIGINALS_DS@$SNAP"
sudo zfs snapshot "$MEDIA_DS@$SNAP"
sudo zfs snapshot "$FLASH_DS@$SNAP"

RSYNC_OPTS=(-aH --delete "--max-delete=$MAX_DELETE" --partial-dir=.rsync-partial
            --stats --human-readable -e "$SSH_CMD"
            --exclude='.audit/' --exclude='.audit/***' --exclude='.rsync-partial/')
[ "$DRY_RUN" = 1 ] && RSYNC_OPTS+=(--dry-run)
# NOTE: deliberately NO --numeric-ids — pushing as ${SSH_USER}@vv (non-root) makes
# files land owned by vv's own faeton, which is exactly the perm story vv wants.

ORIG_VIEW="$ORIGINALS/.zfs/snapshot/$SNAP"
MEDIA_VIEW="$MEDIA/.zfs/snapshot/$SNAP"

log "rsync originals -> vv ..."
rsync "${RSYNC_OPTS[@]}" "$ORIG_VIEW/" "$REMOTE:$VV_ROOT/originals/"

# library = uploaded-asset originals (irreplaceable). profile + upload are small.
# encoded-video/ + thumbs/ + media/backups/ are intentionally NOT mirrored.
for sub in library profile upload; do
  if [ -d "$MEDIA_VIEW/$sub" ]; then
    log "rsync media/$sub -> vv ..."
    rsync "${RSYNC_OPTS[@]}" "$MEDIA_VIEW/$sub/" "$REMOTE:$VV_ROOT/media/$sub/"
  fi
done

# immy state + sidecars (flash pool), from the flash snapshot view. A SQLite file
# inside these trees would be crash-consistent here (atomic snapshot incl. WAL).
for src in "$IMMY_STATE" "$IMMY_SIDECARS"; do
  name="$(basename "$src")"
  view="$FLASH_MOUNT/.zfs/snapshot/$SNAP/${src#"$FLASH_MOUNT"/}"
  [ -d "$view" ] || { log "ERROR: snapshot view $view missing"; exit 1; }
  dest="$VV_ROOT/immy/$name"
  if [ "$DRY_RUN" != 1 ]; then
    $SSH_CMD "$REMOTE" "mkdir -p '$dest'" || { log "ERROR: cannot create vv:$dest"; exit 1; }
  elif ! $SSH_CMD "$REMOTE" "test -d '$dest'"; then
    log "DRY_RUN: vv:$dest does not exist yet; would create + full-copy $src"
    continue
  fi
  log "rsync immy/$name -> vv ..."
  rsync "${RSYNC_OPTS[@]}" "$view/" "$REMOTE:$dest/"
done

# === 4. push dump + recipe to vv (AFTER files) ==============================
log "rsync dump + recipe -> vv:db ..."
DUMP_OPTS=(-aH --partial --stats --human-readable -e "$SSH_CMD")
[ "$DRY_RUN" = 1 ] && DUMP_OPTS+=(--dry-run)
rsync "${DUMP_OPTS[@]}" "$DUMP" "$RECIPE" "$REMOTE:$VV_ROOT/db/"

# manifest copy (staged above; a single file, so no --delete)
if [ "$DRY_RUN" != 1 ]; then
  $SSH_CMD "$REMOTE" "mkdir -p '$VV_ROOT/immy/manifest'" || { log "ERROR: cannot create vv:immy/manifest"; exit 1; }
fi
log "rsync manifest.sqlite -> vv:immy/manifest ..."
if [ "$DRY_RUN" = 1 ] && ! $SSH_CMD "$REMOTE" "test -d '$VV_ROOT/immy/manifest'"; then
  log "DRY_RUN: vv:immy/manifest does not exist yet; would create + copy"
else
  rsync "${DUMP_OPTS[@]}" "$MANIFEST_COPY" "$REMOTE:$VV_ROOT/immy/manifest/"
fi

# prune old dumps on vv (keep newest KEEP_DUMPS); skip in dry-run
if [ "$DRY_RUN" != 1 ]; then
  $SSH_CMD "$REMOTE" "ls -1t '$VV_ROOT/db'/immich-*.sql.gz 2>/dev/null | tail -n +$((KEEP_DUMPS + 1)) | xargs -r rm -f" \
    || log "WARN: vv dump prune failed"
fi

log "all steps complete"
# on_exit trap sends the success ping
