# Nightly backup mirror — n5 → vv

Implements Phase 3 of `raw/PRIMARY-SWAP.md`: **n5** (TrueNAS, Spain) is the live
Immich; **vv** (Synology, Portugal) is a cold file+dump backup, and vv↔n5 is the
offsite leg of 3-2-1. `nightly-mirror.sh` keeps vv a byte-level standby.

## What it does, in order
1. **Perm self-heal** — `originals/` → `faeton:faeton 755/644` (fixes only
   mismatches). Immich runs as root and keeps writing root-locked files; without
   this a faeton-run mirror silently *skips* what it can't read.
2. **pg_dump first** — `pg_dumpall | gzip` → `backups/nightly/`, integrity- and
   size-checked, with a self-describing `RESTORE-RECIPE.txt` (image tags + steps).
   Dump-before-files: a DB referencing missing files is worse than extra files.
3. **Snapshot + mirror files** — atomic ZFS snapshot, then rsync from the
   `.zfs/snapshot/<snap>/` view (immune to concurrent writes) → vv:
   `originals/` and `media/{library,profile,upload}/`.
   `encoded-video/`, `thumbs/`, `media/backups/` are **not** mirrored — they
   regenerate. The snapshot is destroyed after the run.
4. **Push dump → vv:db/** (after files, so vv's dump never references files not
   yet mirrored), then prune to the newest `KEEP_DUMPS`.

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
