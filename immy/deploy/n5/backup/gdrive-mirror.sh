#!/usr/bin/env bash
# Daily Immich DR mirror: n5 -> Google Drive (second offsite copy, Finder-browseable).
#
# Design: docs/superpowers/specs/2026-10-12-gdrive-mirror-design.md. Drive holds
# the same scope as vv (guards-lib scope_patterns): every photo/RAW, the named
# trip dirs in full, no video under the dated YYYY/ tree, no .lrv/.lrf proxies.
#   - originals + media/{library,profile,upload} go up PLAIN, so Google Drive
#     for Desktop can stream them in Finder;
#   - the Postgres dump, restore recipe, immy state/sidecars and the dedup
#     manifest go through an rclone `crypt` remote (useless in Finder, and the
#     dump is the one file that holds faces + GPS + captions of everything).
#
# It never dumps the DB itself: nightly-mirror.sh (05:00) makes the dump and the
# consistent manifest copy; this job (08:00) refuses to run on a stale dump.
#
# Order is load-bearing, as in nightly-mirror.sh: files reach Drive before the
# dump that references them. Every sync moves what it replaces or deletes into
# Immich-DR/_history/<run>/ (server-side), pruned after HISTORY_DAYS.
#
# Google caps uploads at 750 GB/day. A run stopped by --max-transfer or the
# upload limit is a partial success ("SEEDING"); the next run resumes.
#
# Usage:
#   DRY_RUN=1 ./gdrive-mirror.sh     # rclone --dry-run, nothing written to Drive
#   ./gdrive-mirror.sh               # live (what cron runs)
#   VERIFY=1 ./gdrive-mirror.sh      # also hash-check originals against Drive (monthly)

set -euo pipefail

# --- config -----------------------------------------------------------------
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=guards-lib.sh
. "$SELF_DIR/guards-lib.sh"   # count_files / mirror_exclude_dirs / scope_rclone_filter
GDRIVE_ENV="${GDRIVE_ENV:-$SELF_DIR/gdrive.env}"
# DRY_RUN / VERIFY passed on the command line MUST win over gdrive.env.
_CLI_DRY_RUN="${DRY_RUN:-}"
_CLI_VERIFY="${VERIFY:-}"
# shellcheck source=/dev/null
[ -f "$GDRIVE_ENV" ] && . "$GDRIVE_ENV"
[ -n "$_CLI_DRY_RUN" ] && DRY_RUN="$_CLI_DRY_RUN"
[ -n "$_CLI_VERIFY" ] && VERIFY="$_CLI_VERIFY"

HC_URL="${HC_URL:-}"                                   # Healthchecks ping URL (empty = no pings)
HC_CURL_OPTS="${HC_CURL_OPTS:-}"                       # extra curl flags for hc()
NOTIFY_CMD="${NOTIFY_CMD:-}"                           # optional: run as `$NOTIFY_CMD "<message>"` on failure
RID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || true)"

RCLONE="${RCLONE:-rclone}"
RCLONE_CONFIG="${RCLONE_CONFIG:-$HOME/.config/rclone/rclone.conf}"
export RCLONE_CONFIG
DRIVE_REMOTE="${DRIVE_REMOTE:-gdrive:}"                # plain Drive remote (type drive)
DRIVE_ROOT="${DRIVE_ROOT:-Immich-DR}"                  # folder on Drive
CRYPT_REMOTE="${CRYPT_REMOTE:-gdrive-dr-crypt:}"       # type crypt, remote = <DRIVE_REMOTE><DRIVE_ROOT>/_private

ORIGINALS="${ORIGINALS:-/mnt/tank/immich/originals}"
MEDIA="${MEDIA:-/mnt/tank/immich/media}"
ORIGINALS_DS="${ORIGINALS_DS:-tank/immich/originals}"
MEDIA_DS="${MEDIA_DS:-tank/immich/media}"
FLASH_DS="${FLASH_DS:-flash}"
FLASH_MOUNT="${FLASH_MOUNT:-/mnt/flash}"
IMMY_STATE="${IMMY_STATE:-/mnt/flash/immy/state}"
IMMY_SIDECARS="${IMMY_SIDECARS:-/mnt/flash/immy/sidecars}"
DUMP_DIR="${DUMP_DIR:-/mnt/tank/immich/backups/nightly}"           # written by nightly-mirror.sh
MANIFEST_COPY="${MANIFEST_COPY:-$DUMP_DIR/manifest-stage/manifest.sqlite}"

SKIP_DATED_VIDEO="${SKIP_DATED_VIDEO:-1}"              # same scope as vv; 0 = all video too
KEEP_DUMPS="${KEEP_DUMPS:-7}"                          # dumps kept in crypt:db/
MAX_DUMP_AGE_H="${MAX_DUMP_AGE_H:-26}"                 # refuse a dump older than this
MAX_DELETE="${MAX_DELETE:-200}"                        # per sync; deletions go to _history anyway
MAX_TRANSFER="${MAX_TRANSFER:-700G}"                   # per run; Google's cap is 750 GB/day
PRIVATE_RESERVE="${PRIVATE_RESERVE:-20G}"              # part of MAX_TRANSFER the file steps may not use,
                                                       # so dump + immy state still go up while seeding
MIN_DRIVE_FREE_GB="${MIN_DRIVE_FREE_GB:-300}"          # refuse to run below this much free
HISTORY_DAYS="${HISTORY_DAYS:-60}"
MIN_ORIGINALS_FILES="${MIN_ORIGINALS_FILES:-1000}"     # empty-source guard for the originals sync
MIN_STATE_FILES="${MIN_STATE_FILES:-1}"
TPSLIMIT="${TPSLIMIT:-8}"
TRANSFERS="${TRANSFERS:-4}"
BWLIMIT="${BWLIMIT:-}"                                 # e.g. 20M; empty = no limit
DRY_RUN="${DRY_RUN:-0}"
VERIFY="${VERIFY:-0}"

LOCKFILE="${LOCKFILE:-$SELF_DIR/.gdrive-mirror.lock}"
LOG_DIR="${LOG_DIR:-$SELF_DIR/logs}"
KEEP_LOG_DAYS="${KEEP_LOG_DAYS:-30}"

# --- single-instance lock ----------------------------------------------------
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  echo "another gdrive mirror run holds $LOCKFILE; exiting." >&2
  exit 0
fi

# --- logging ----------------------------------------------------------------
mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/gdrive-$TS.log"
exec > >(tee -a "$LOG") 2>&1
log() { echo "[$(date +%H:%M:%S)] $*"; }

hc() {  # hc <path-suffix> [extra curl args...]
  [ -n "$HC_URL" ] || return 0
  local url="${HC_URL}${1}"
  if [ -n "$RID" ]; then case "$1" in *\?*) url="${url}&rid=${RID}";; *) url="${url}?rid=${RID}";; esac; fi
  # shellcheck disable=SC2086 # HC_CURL_OPTS is intentionally word-split
  curl -fsS -m 10 --retry 3 ${HC_CURL_OPTS} "${url}" "${@:2}" >/dev/null 2>&1 || true
}

SNAP="gdrive-$TS"
# Work dir on the dump's filesystem, so the handoff files can be pinned by hardlink.
WORK="$(mktemp -d "$DUMP_DIR/.gdrive-work.XXXXXX")"
cleanup() {
  local ds
  for ds in "$ORIGINALS_DS" "$MEDIA_DS" "$FLASH_DS"; do
    if sudo zfs list -H -t snapshot "$ds@$SNAP" >/dev/null 2>&1; then
      sudo zfs destroy "$ds@$SNAP" 2>/dev/null || log "WARN: could not destroy $ds@$SNAP"
    fi
  done
  rm -rf "$WORK"
}

RESULT="FAILED"   # set to COMPLETE or SEEDING by the run
on_exit() {
  local rc=$?
  cleanup
  if [ "$rc" -eq 0 ]; then
    log "$RESULT"
    hc ""
  else
    log "FAILED (exit $rc)"
    hc /fail --data-raw "$(tail -c 5000 "$LOG")"
    if [ -n "$NOTIFY_CMD" ]; then
      "$NOTIFY_CMD" "gdrive-mirror FAILED (exit $rc) on $(hostname): $(grep -m1 'ERROR' "$LOG" | cut -c1-300) — log: $LOG" \
        >/dev/null 2>&1 || true
    fi
  fi
  find "$LOG_DIR" -name 'gdrive-*.log' -type f -mtime "+$KEEP_LOG_DAYS" -delete 2>/dev/null || true
}
trap on_exit EXIT
# Without these a SIGTERM can reach the EXIT trap with $? = 0 and ping success.
trap 'exit 143' TERM
trap 'exit 130' INT

hc /start
log "gdrive mirror start (DRY_RUN=$DRY_RUN VERIFY=$VERIFY SKIP_DATED_VIDEO=$SKIP_DATED_VIDEO) -> ${DRIVE_REMOTE}${DRIVE_ROOT}"

DEST="${DRIVE_REMOTE}${DRIVE_ROOT}"
HIST="$DEST/_history/$TS"

# --- preflight ---------------------------------------------------------------
log "preflight..."
command -v "$RCLONE" >/dev/null || { log "ERROR: rclone not found"; exit 1; }
for r in "$DRIVE_REMOTE" "$CRYPT_REMOTE"; do
  "$RCLONE" listremotes | grep -qxF "$r" || { log "ERROR: rclone remote $r not configured in $RCLONE_CONFIG"; exit 1; }
done
sudo zfs list -H "$ORIGINALS_DS" "$MEDIA_DS" "$FLASH_DS" >/dev/null \
  || { log "ERROR: datasets $ORIGINALS_DS / $MEDIA_DS / $FLASH_DS missing"; exit 1; }
for d in "$IMMY_STATE" "$IMMY_SIDECARS"; do
  case "$d" in "$FLASH_MOUNT"/*) ;; *) log "ERROR: $d is not under FLASH_MOUNT=$FLASH_MOUNT"; exit 1;; esac
  [ -d "$d" ] || { log "ERROR: $d missing"; exit 1; }
done
# newest dump (made by nightly-mirror.sh) must be fresh and pass gzip -t
DUMP="$(ls -1t "$DUMP_DIR"/immich-*.sql.gz 2>/dev/null | head -n 1 || true)"
[ -n "$DUMP" ] || { log "ERROR: no dump in $DUMP_DIR"; exit 1; }
dump_age_h=$(( ( $(date +%s) - $(stat -c %Y "$DUMP") ) / 3600 ))
[ "$dump_age_h" -le "$MAX_DUMP_AGE_H" ] \
  || { log "ERROR: newest dump $DUMP is ${dump_age_h}h old (> ${MAX_DUMP_AGE_H}h); did nightly-mirror.sh fail?"; exit 1; }
gzip -t "$DUMP" || { log "ERROR: $DUMP fails gzip -t"; exit 1; }
[ -s "$DUMP_DIR/RESTORE-RECIPE.txt" ] || { log "ERROR: $DUMP_DIR/RESTORE-RECIPE.txt missing"; exit 1; }
[ -s "$MANIFEST_COPY" ] || { log "ERROR: manifest copy $MANIFEST_COPY missing (made by nightly-mirror.sh)"; exit 1; }
# Drive quota: free = total - used (incl. trash); about --json gives bytes
drive_free_gb() {
  "$RCLONE" about "$DRIVE_REMOTE" --json 2>/dev/null \
    | python3 -c 'import json,sys; print(int(json.load(sys.stdin).get("free", -1) // 1e9))' 2>/dev/null || echo -1
}
# _history/<run> dirs older than HISTORY_DAYS: permanent delete so quota frees.
# Runs BEFORE the free-space floor, or a full Drive could never recover itself.
prune_history() {
  local cutoff d
  cutoff="$(date -d "-$HISTORY_DAYS days" +%Y%m%d-%H%M%S)"
  while IFS= read -r d; do
    d="${d%/}"
    [[ "$d" =~ ^[0-9]{8}-[0-9]{6}$ ]] || continue
    if [[ "$d" < "$cutoff" ]]; then
      log "prune _history/$d"
      "$RCLONE" purge "$DEST/_history/$d" --drive-use-trash=false || log "WARN: could not prune _history/$d"
    fi
  done < <("$RCLONE" lsf --dirs-only "$DEST/_history" 2>/dev/null || true)
}
[ "$DRY_RUN" = 1 ] || prune_history
free_gb=$(drive_free_gb)
[ "$free_gb" -ge "$MIN_DRIVE_FREE_GB" ] \
  || { log "ERROR: Drive free ${free_gb}GB < ${MIN_DRIVE_FREE_GB}GB floor (or quota unreadable); refusing"; exit 1; }
# Pin this run's handoff bundle: nightly-mirror.sh may replace the manifest,
# recipe or newest dump while we upload (separate locks). Hardlinks keep the
# exact inodes checked above; the recipe is tiny, so it is copied.
mkdir -p "$WORK/handoff"
ln "$DUMP" "$WORK/handoff/$(basename "$DUMP")" || { log "ERROR: cannot pin $DUMP"; exit 1; }
ln "$MANIFEST_COPY" "$WORK/handoff/manifest.sqlite" || { log "ERROR: cannot pin $MANIFEST_COPY"; exit 1; }
cp "$DUMP_DIR/RESTORE-RECIPE.txt" "$WORK/handoff/RESTORE-RECIPE.txt"
DUMP="$WORK/handoff/$(basename "$DUMP")"
log "preflight OK (Drive free: ${free_gb}GB, dump: $(basename "$DUMP"), ${dump_age_h}h old)"

# --- snapshot + empty-source guards -----------------------------------------
log "zfs snapshot @$SNAP ..."
sudo zfs snapshot "$ORIGINALS_DS@$SNAP"
sudo zfs snapshot "$MEDIA_DS@$SNAP"
sudo zfs snapshot "$FLASH_DS@$SNAP"
ORIG_VIEW="$ORIGINALS/.zfs/snapshot/$SNAP"
MEDIA_VIEW="$MEDIA/.zfs/snapshot/$SNAP"
flash_view() { printf '%s/.zfs/snapshot/%s/%s' "$FLASH_MOUNT" "$SNAP" "${1#"$FLASH_MOUNT"/}"; }

# Filters: one per tree kind, from the same lists the vv mirror uses.
for kind in originals media state; do
  scope_rclone_filter "$kind" "$SKIP_DATED_VIDEO" > "$WORK/$kind.filter" \
    || { log "ERROR: could not build $kind filter"; exit 1; }
done

# rclone sync from an empty/unreadable view would move Drive's good copy into
# _history (recoverable, but a mess): count first, fail closed on any error.
# The originals count uses the generated filter itself, so it counts exactly
# what the sync would keep (a tree of only out-of-scope videos counts as 0).
mapfile -t EXCL_STATE < <(mirror_exclude_dirs state)
n_orig=$("$RCLONE" size --json --filter-from "$WORK/originals.filter" "$ORIG_VIEW/" 2>/dev/null \
  | python3 -c 'import json,sys; print(int(json.load(sys.stdin)["count"]))') \
  || { log "ERROR: cannot count $ORIG_VIEW; aborting (fail closed)"; exit 1; }
[ "$n_orig" -ge "$MIN_ORIGINALS_FILES" ] \
  || { log "ERROR: snapshot view of originals has only $n_orig files (< $MIN_ORIGINALS_FILES); refusing"; exit 1; }
# media/library holds uploaded-asset originals: it must exist and be non-empty.
# profile/ and upload/ are tiny and may legitimately be empty or absent.
[ -d "$MEDIA_VIEW/library" ] || { log "ERROR: $MEDIA_VIEW/library missing; refusing"; exit 1; }
n=$(count_files "$MEDIA_VIEW/library" 1 .rsync-partial) \
  || { log "ERROR: cannot count $MEDIA_VIEW/library; aborting (fail closed)"; exit 1; }
[ "$n" -ge 1 ] || { log "ERROR: media/library snapshot view is empty; refusing"; exit 1; }
for src in "$IMMY_STATE" "$IMMY_SIDECARS"; do
  n=$(count_files "$(flash_view "$src")" "$MIN_STATE_FILES" "${EXCL_STATE[@]}") \
    || { log "ERROR: cannot count $(flash_view "$src"); aborting (fail closed)"; exit 1; }
  [ "$n" -ge "$MIN_STATE_FILES" ] || { log "ERROR: $src snapshot view is empty; refusing"; exit 1; }
done

# --- rclone plumbing ---------------------------------------------------------
COMMON=(--fast-list --tpslimit "$TPSLIMIT" --transfers "$TRANSFERS" --checkers 8
        --drive-stop-on-upload-limit --drive-skip-shortcuts --drive-skip-gdocs
        --stats 5m --stats-one-line --stats-log-level NOTICE --log-level NOTICE)
[ -n "$BWLIMIT" ] && COMMON+=(--bwlimit "$BWLIMIT")
[ "$DRY_RUN" = 1 ] && COMMON+=(--dry-run)
# One transfer budget for the whole run: each step gets what is left. The file
# steps (big, resumable) leave PRIVATE_RESERVE for the private steps (small,
# and the most valuable thing to have offsite while the seed is still running).
budget_bytes=$(numfmt --from=iec "${MAX_TRANSFER%B}") \
  || { log "ERROR: bad MAX_TRANSFER '$MAX_TRANSFER'"; exit 1; }
reserve_bytes=$(numfmt --from=iec "${PRIVATE_RESERVE%B}") \
  || { log "ERROR: bad PRIVATE_RESERVE '$PRIVATE_RESERVE'"; exit 1; }
[ "$reserve_bytes" -lt "$budget_bytes" ] \
  || { log "ERROR: PRIVATE_RESERVE ($PRIVATE_RESERVE) must be below MAX_TRANSFER ($MAX_TRANSFER)"; exit 1; }
CAPPED=0

# run_rclone STEP KEEP_BYTES rclone-args... ; the step may use the budget minus
# KEEP_BYTES. rc 0 = done; exit 8 / upload limit = capped (SEEDING, resumes).
run_rclone() {
  local step="$1" keep="$2"; shift 2
  local rc=0 out="$WORK/$step.json" limit=$(( budget_bytes - keep ))
  if [ "$limit" -le 0 ]; then
    log "$step: transfer budget used up this run; deferred"; CAPPED=1; return 0
  fi
  "$RCLONE" "$@" "${COMMON[@]}" --max-transfer "$limit" --cutoff-mode cautious \
    --use-json-log --log-file "$out" || rc=$?
  # surface rclone's errors/warnings in our log (bounded); dry-run's per-file
  # "Skipped ... as --dry-run is set" notices are only counted
  python3 - "$out" <<'PY' || true
import json, sys
shown = skipped = 0
for line in open(sys.argv[1], errors="replace"):
    try: m = json.loads(line)
    except ValueError: continue
    msg = m.get("msg", "")
    if "as --dry-run is set" in msg:
        skipped += 1; continue
    if m.get("level") in ("error", "warning", "notice") and not m.get("stats") and shown < 50:
        print("  rclone:", msg[:300]); shown += 1
if skipped:
    print(f"  rclone: dry-run: {skipped} operation(s) skipped")
PY
  local sent
  sent=$(python3 - "$out" <<'PY' || echo 0
import json, sys
best = 0
for line in open(sys.argv[1], errors="replace"):
    try: m = json.loads(line)
    except ValueError: continue
    st = m.get("stats") or {}
    best = max(best, int(st.get("bytes", 0) or 0))
print(best)
PY
)
  # dry-run neither transfers nor enforces --max-transfer: don't spend budget
  [ "$DRY_RUN" = 1 ] || budget_bytes=$(( budget_bytes - sent ))
  if [ "$rc" -eq 0 ]; then
    log "$step: OK ($(numfmt --to=iec "$sent") $([ "$DRY_RUN" = 1 ] && echo "would be sent" || echo sent))"; return 0
  fi
  if [ "$rc" -eq 8 ]; then
    # our own --max-transfer: the rest of the budget (the private reserve) stays
    log "$step: stopped at this run's transfer budget ($(numfmt --to=iec "$sent") sent); resumes next run"
    CAPPED=1; return 0
  fi
  if grep -qi 'upload limit' "$out" 2>/dev/null; then
    # rclone reports Google's daily cap AND a full Drive the same way: tell
    # them apart by the quota itself. Only the daily cap is a partial success.
    local f; f=$(drive_free_gb)
    if [ "$f" -ge 0 ] && [ "$f" -ge "$MIN_DRIVE_FREE_GB" ]; then
      log "$step: Google's daily upload cap reached ($(numfmt --to=iec "$sent") sent); resumes next run"
      CAPPED=1; budget_bytes=0; return 0
    fi
    log "ERROR: $step: upload refused and Drive free is ${f}GB — storage quota, not the daily cap"; return 1
  fi
  log "ERROR: $step: rclone exit $rc"; return 1
}

# === 1. files (plain) ========================================================
log "sync originals -> $DEST/originals ..."
run_rclone originals "$reserve_bytes" sync "$ORIG_VIEW/" "$DEST/originals" \
  --filter-from "$WORK/originals.filter" --delete-excluded --max-delete "$MAX_DELETE" \
  --backup-dir "$HIST/originals"

for sub in library profile upload; do
  [ -d "$MEDIA_VIEW/$sub" ] || { log "WARN: $MEDIA_VIEW/$sub missing; skipped"; continue; }
  log "sync media/$sub -> $DEST/media/$sub ..."
  run_rclone "media-$sub" "$reserve_bytes" sync "$MEDIA_VIEW/$sub/" "$DEST/media/$sub" \
    --filter-from "$WORK/media.filter" --max-delete "$MAX_DELETE" \
    --backup-dir "$HIST/media/$sub"
done

# === 2. private (crypt), after the files it references =======================
# Most valuable first: dump + recipe, then the manifest, then immy state.
log "copy dump + recipe + manifest -> ${CRYPT_REMOTE} ..."
run_rclone dump 0 copy "$DUMP" "${CRYPT_REMOTE}db"
run_rclone recipe 0 copyto "$WORK/handoff/RESTORE-RECIPE.txt" "${CRYPT_REMOTE}db/RESTORE-RECIPE.txt"
run_rclone manifest 0 copyto "$WORK/handoff/manifest.sqlite" "${CRYPT_REMOTE}immy/manifest/manifest.sqlite"
for src in "$IMMY_STATE" "$IMMY_SIDECARS"; do
  name="$(basename "$src")"
  log "sync immy/$name -> ${CRYPT_REMOTE}immy/$name ..."
  run_rclone "immy-$name" 0 sync "$(flash_view "$src")/" "${CRYPT_REMOTE}immy/$name" \
    --filter-from "$WORK/state.filter" --max-delete "$MAX_DELETE"
done

# === 3. housekeeping (skipped in dry-run) ====================================
if [ "$DRY_RUN" != 1 ]; then
  # keep the newest KEEP_DUMPS dumps (names sort by timestamp)
  mapfile -t dumps < <("$RCLONE" lsf --files-only --include 'immich-*.sql.gz' "${CRYPT_REMOTE}db" | sort)
  if [ "${#dumps[@]}" -gt "$KEEP_DUMPS" ]; then
    for old in "${dumps[@]:0:${#dumps[@]}-KEEP_DUMPS}"; do
      log "prune crypt:db/$old"
      "$RCLONE" deletefile "${CRYPT_REMOTE}db/$old" --drive-use-trash=false || log "WARN: could not prune $old"
    done
  fi
fi

# === 4. verify (monthly) =====================================================
if [ "$VERIFY" = 1 ] && [ "$CAPPED" = 0 ]; then
  log "verify: rclone check (MD5, one-way) originals ..."
  "$RCLONE" check "$ORIG_VIEW/" "$DEST/originals" --one-way --fast-list \
    --filter-from "$WORK/originals.filter" --checkers 8 --tpslimit "$TPSLIMIT" \
    --log-level NOTICE || { log "ERROR: verify found differences"; exit 1; }
  log "verify: OK"
elif [ "$VERIFY" = 1 ]; then
  log "verify: skipped (still seeding)"
fi

# === 5. status ===============================================================
if [ "$CAPPED" = 1 ]; then RESULT="SEEDING (transfer cap hit; next run resumes)"; else RESULT="COMPLETE"; fi
if [ "$DRY_RUN" != 1 ]; then
  {
    echo "Immich DR mirror on Google Drive — written by gdrive-mirror.sh on n5"
    echo "run:         $TS"
    echo "result:      $RESULT"
    echo "verified:    $([ "$VERIFY" = 1 ] && [ "$CAPPED" = 0 ] && echo "yes (MD5, this run)" || echo "no (monthly)")"
    echo "scope:       photos/RAW everywhere; named trip dirs in full; no video under dated YYYY/ (SKIP_DATED_VIDEO=$SKIP_DATED_VIDEO); no .lrv/.lrf"
    echo "originals/   plain mirror of $ORIGINALS"
    echo "media/       plain mirror of $MEDIA/{library,profile,upload}"
    echo "_private/    rclone crypt: db/ (pg_dumpall + RESTORE-RECIPE.txt), immy/ (state, sidecars, manifest)"
    echo "_history/    files replaced or deleted by a sync, kept $HISTORY_DAYS days"
    echo "Drive is written only by n5; do not save into this folder from a Mac."
  } > "$WORK/MIRROR-STATUS.txt"
  "$RCLONE" copyto "$WORK/MIRROR-STATUS.txt" "$DEST/MIRROR-STATUS.txt" || log "WARN: status file upload failed"
fi
log "all steps done"
