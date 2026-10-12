# Nightly backup mirror — n5 → vv

Implements Phase 3 of `raw/PRIMARY-SWAP.md`: **n5** (TrueNAS) is the live
Immich; **vv** (Synology, offsite) is a cold file+dump backup, and vv↔n5 is the
offsite leg of 3-2-1. `nightly-mirror.sh` keeps vv a byte-level standby.

## What it does, in order
1. **Perm self-heal** — `originals/` → `faeton:faeton 755/644` (fixes only
   mismatches). Immich runs as root and keeps writing root-locked files; without
   this a faeton-run mirror silently *skips* what it can't read.
2. **pg_dump first** — `pg_dumpall | gzip` → `backups/nightly/`, integrity- and
   size-checked, with a self-describing `RESTORE-RECIPE.txt` (image tags + steps).
   Dump-before-files: a DB referencing missing files is worse than extra files.
   Also copies the dedup `manifest.sqlite` (live WAL DB, `/mnt/tank/media/state/`)
   into a faeton-owned staging dir with `sudo sqlite3 .backup`, then requires
   `PRAGMA integrity_check` = `ok`. A live SQLite file is never copied raw. The source is opened `-readonly`, must be
   non-empty, and the copy must have the `asset`/`cluster` tables and at least
   `MANIFEST_MIN_ROWS` asset rows or the run aborts before anything reaches vv.
3. **Snapshot + mirror files** — atomic ZFS snapshot, then rsync from the
   `.zfs/snapshot/<snap>/` view (immune to concurrent writes) → vv:
   `originals/` and `media/{library,profile,upload}/`.
   `encoded-video/`, `thumbs/`, `media/backups/` are **not** mirrored — they
   regenerate. The snapshot is destroyed after the run.
   immy's own state is mirrored from a transient snapshot of the **`flash`**
   dataset (the pool-root dataset; `/mnt/flash/immy/{state,sidecars}` are plain
   directories in it, not child datasets) → vv `immy/state/`, `immy/sidecars/`.
   Without this they had no backup at all.
4. **Push dump → vv:db/** and the manifest copy → vv `immy/manifest/` (after
   files, so vv's dump never references files not yet mirrored), then prune to the newest `KEEP_DUMPS`.

Heartbeats to **Healthchecks** (`/start`, success, `/fail` with the log tail) so a
*missed* run alerts, not just a failed one. `flock` prevents overlap. Logs in
`/mnt/tank/scripts/logs/`.

## Why a script and not native TrueNAS tasks
Native tools cover *pieces* but not the orchestration:
- **ZFS Replication** is the native mirror — but it needs a **ZFS receiver**. vv
  is a Synology (btrfs); it cannot receive a ZFS stream. Rules out the most
  native option entirely.
- **Rsync Task** (SCALE) is just an rsync — no pre/post hooks, so it can't
  sequence dump → perm-fix → snapshot → ping around the copy, and it rsyncs the
  live dataset, not a `.zfs` snapshot view.
- **pg_dump** has no native task at all.

So the orchestration lives in this one script, and we lean on TrueNAS for the
parts it does well: the **Cron Job** runs it, and the existing **Periodic
Snapshot Tasks** keep snapshot history independently. (The transient
`mirror-<ts>` snapshot this script makes is only a consistency anchor.)

## Install (on n5, as faeton)
The install dir must be faeton-writable (the script keeps its lock + `logs/`
beside itself). `/mnt/tank/scripts` is root-owned, so use a faeton-owned subdir:
```sh
DIR=/mnt/tank/scripts/immich-mirror
sudo install -d -o faeton -g faeton "$DIR"
install -m755 nightly-mirror.sh "$DIR/"
cp mirror.env.example "$DIR/mirror.env"
# edit mirror.env → paste HC_URL (the base check URL; script adds /start, /fail)
$EDITOR "$DIR/mirror.env"
```

Dry-run first (rsync `--dry-run`, no writes/deletes on vv):
```sh
DRY_RUN=1 /mnt/tank/scripts/immich-mirror/nightly-mirror.sh
```
Confirm: preflight passes, perm-fix counts look sane, the dump validates, rsync
itemizes only expected deltas, and `--max-delete` is not tripped.

Register the cron job natively (05:00 Europe/Lisbon, after the overnight immy
window — adjust the schedule to taste):
```sh
sudo midclt call cronjob.create '{
  "command": "/mnt/tank/scripts/immich-mirror/nightly-mirror.sh",
  "description": "Immich nightly mirror n5->vv",
  "enabled": true, "stdout": false, "stderr": false, "user": "faeton",
  "schedule": {"minute":"0","hour":"5","dom":"*","month":"*","dow":"*"}
}'
sudo midclt call cronjob.query   # verify; also visible in the SCALE UI
```

## Backup roots (as of 2026-10-02)
| Source | Method | vv destination |
|---|---|---|
| `tank/immich/originals` | ZFS snapshot view, rsync `--delete` | `originals/` |
| `tank/immich/media/{library,profile,upload}` | ZFS snapshot view | `media/<sub>/` |
| `/mnt/flash/immy/state` | `flash` snapshot view | `immy/state/` |
| `/mnt/flash/immy/sidecars` | `flash` snapshot view | `immy/sidecars/` |
| `/mnt/tank/media/state/manifest.sqlite` | `sqlite3 .backup` -> staging -> rsync | `immy/manifest/` |
| pg_dumpall + recipe | gzip, verified | `db/` |

**Scope** (`SKIP_DATED_VIDEO=1`, default since 2026-07-14): vv got full once
and is shared, so video under the dated `YYYY/` tree — phone video that also
lives in iCloud / Google Photos — is not mirrored, and `.lrv`/`.lrf` proxies
never are. Photos/RAW everywhere and the named trip dirs (`YYYY-MM-name/`) ship
in full. The list lives in `guards-lib.sh` (`scope_patterns`) and is shared with
the Drive mirror below, so both offsite copies hold the same set; a preflight
also refuses to run when vv has less than `MIN_VV_FREE_GB` free.

Upgrading an existing install: the script now needs `sudo zfs snapshot` rights
on `flash` too, and `sqlite3` on PATH (preflight checks both). New vv dirs under
`immy/` are created by the first live run; a `DRY_RUN=1` run reports "would
create" instead (it never writes to vv). Install-time note: the installed copy at
`/mnt/tank/scripts/immich-mirror/` is separate from this repo file — copy the
script over and dry-run before trusting it.

Fail-closed guards: before any `--delete` transfer of immy state/sidecars the
snapshot view must contain at least `MIN_STATE_FILES` files; an empty view is
refused if the live dir is non-empty or vv still holds files (override only with
`ALLOW_EMPTY_SOURCES=1`). An empty source with an empty vv copy (sidecars today)
is simply skipped. The guard counts with the same excludes the rsync uses
(`mirror_exclude_dirs` in `guards-lib.sh`), so it counts exactly what will be
transferred.

Excludes are per tree: `.rsync-partial/` everywhere; `.audit/` from the
**originals** tree only. All of immy's per-trip state (journals, markers,
offline caches, derivatives) lives under `state/<trip>/.audit/`, so the state
tree is mirrored with `.audit/` included (an earlier version excluded it
globally and shipped nothing). The first live run after this change copies the
whole state tree (~9 GB); routine churn there (offline caches pruned after
sync) counts against `MAX_DELETE`, and rsync exits non-zero (run fails loudly)
if a night's deletions exceed it.

Counting is fail-closed too: `guards-lib.sh` (sourced by the script — copy it
next to `nightly-mirror.sh` on install) returns non-zero with no output on any
find/ssh error, and the script aborts instead of reading that as "empty".
`bash tests/test-guards.sh` exercises this with stubbed `ssh`/`find`.

## Config
All knobs live in `mirror.env` (see `mirror.env.example`). Only `HC_URL` must be
set; everything else defaults to the as-built n5 layout.

## Ops
- **Logs**: `/mnt/tank/scripts/logs/mirror-<ts>.log` (pruned after `KEEP_LOG_DAYS`).
- **Run as root instead of faeton+sudo** (alternative): set the cron `user` to
  `root` and authorize root's key on vv (`SSH_KEY=/root/.ssh/...` in `mirror.env`).
  Default keeps it as faeton because faeton's vv key already works.
- **3rd-location dump copy** (optional, for true 3-2-1): a TrueNAS *Cloud Sync
  Task* on `backups/nightly/*.sql.gz` → B2/S3 is the clean native fit (the dump
  is tiny). Not built here.

## Restore drill — mandatory before trusting vv as a standby
An untested mirror is a copy, not a standby. Rehearse a failback on vv once:
restore the latest `db/immich-*.sql.gz` into a fresh postgres (per
`RESTORE-RECIPE.txt`), start the stack pinned to the recipe's image tags, run the
Phase 2 acceptance checklist (`raw/PRIMARY-SWAP.md`), promote one tiny trip. Keep
vv's compose tracking n5's Immich version after each upgrade so a failback never
loads a newer dump into an older binary.

## Google Drive mirror — `gdrive-mirror.sh`
A second offsite copy of the same scope, on Google Drive, browseable in Finder
through Google Drive for Desktop. Design: `docs/superpowers/specs/2026-10-12-gdrive-mirror-design.md`.

| On Drive (`$DRIVE_ROOT`) | What | Form |
|---|---|---|
| `originals/` | same scope as vv | plain |
| `media/{library,profile,upload}/` | uploaded-asset originals | plain |
| `_private/db/` | newest `KEEP_DUMPS` dumps + `RESTORE-RECIPE.txt` | rclone crypt |
| `_private/immy/` | `state/`, `sidecars/`, `manifest/manifest.sqlite` | rclone crypt |
| `_history/<run>/` | whatever a sync replaced or deleted, `HISTORY_DAYS` | plain |
| `MIRROR-STATUS.txt` | last run, result, scope | plain |

It reuses the 05:00 job's dump and manifest copy (refusing a dump older than
`MAX_DUMP_AGE_H`), so schedule it after `nightly-mirror.sh`. Google caps uploads
at 750 GB/day: one run sends at most `MAX_TRANSFER`, and a run stopped by that
cap is logged `SEEDING` and pinged as success; the next run resumes. The first
seed (~2 TiB) takes 3–4 runs.

Setup (on n5, as faeton):
```sh
# 1. a drive remote (ideally with your own Google Cloud OAuth client_id)
rclone config                       # -> e.g. "gdrive", type drive, scope drive
# 2. the crypt remote over <drive remote><DRIVE_ROOT>/_private
rclone config create gdrive-dr-crypt crypt remote=gdrive:Immich-DR/_private \
  filename_encryption=standard directory_name_encryption=true \
  password="$(openssl rand -base64 30)" password2="$(openssl rand -base64 30)" --obscure
#    store both passwords OUTSIDE n5 too:  rclone config show gdrive-dr-crypt
#    (the values are obscured, not encrypted; `rclone reveal <value>` prints them)
# 3. install beside nightly-mirror.sh (shares guards-lib.sh and logs/)
DIR=/mnt/tank/scripts/immich-mirror
install -m755 gdrive-mirror.sh "$DIR/"; install -m644 guards-lib.sh "$DIR/"
cp gdrive.env.example "$DIR/gdrive.env"; $EDITOR "$DIR/gdrive.env"
DRY_RUN=1 "$DIR/gdrive-mirror.sh"
# 4. cron: daily 08:00, plus a monthly VERIFY run
sudo midclt call cronjob.create '{"command": "/mnt/tank/scripts/immich-mirror/gdrive-mirror.sh",
  "description": "Immich DR mirror n5->Google Drive", "enabled": true, "stdout": false,
  "stderr": false, "user": "faeton", "schedule": {"minute":"0","hour":"8","dom":"*","month":"*","dow":"*"}}'
```
`VERIFY=1` runs `rclone check --one-way` (Drive MD5 vs a local hash of the whole
scope — hours of disk reads) after the sync; do it monthly, not nightly.

On the Mac, install Google Drive for Desktop only after a clean `VERIFY=1` run.
Use streaming mode, never mark `Immich-DR` "available offline", and never save
into it: n5 is the only writer (a Finder deletion is undone by the next sync,
and the old copy is in `_history/`). Quick Look on a video downloads all of it.
