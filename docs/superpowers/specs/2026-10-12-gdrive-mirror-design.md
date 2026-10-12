# Google Drive DR mirror — design

Date: 2026-10-12. Status: approved in conversation, implementing.

## Goal

A second offsite copy of everything irreplaceable in the Immich library, on a
personal Google Drive (5 TiB plan, ~3.56 TiB free), that is

- **complete enough to rebuild Immich** (albums, people, CLIP, captions,
  transcripts) without re-running any ML, together with the plain files, and
- **browseable in macOS Finder** through Google Drive for Desktop (streaming).

Not goals: a third copy of everything (the full 3.6 TiB does not fit), a sync
peer (Drive is written by n5 only), or replacing vv.

## Copies today, and what Drive adds

| Part of the library | Size | n5 | vv | Other | Drive |
|---|---|---|---|---|---|
| Trip dirs (`YYYY-MM-name/`): drone / Insta360 masters, camera RAW | 1.81 TiB | yes | yes | — | **yes** |
| Dated tree (`YYYY/MM/`) stills + XMP | 0.19 TiB | yes | yes | most in iCloud | **yes** |
| Dated tree video (phone video) | 1.66 TiB | yes | no | most in iCloud | no |
| `.lrv` / `.lrf` proxies | 0.05 TiB | yes | no | derivable | no |
| `media/{library,profile,upload}` | 5 GiB | yes | yes | — | **yes** |
| Postgres dump, restore recipe | ~0.75 GiB/day | yes | yes | — | **yes, encrypted** |
| immy `state/`, `sidecars/`, dedup `manifest.sqlite` | ~9.3 GiB | yes | yes (from 2026-10-12) | — | **yes, encrypted** |

The Drive scope is the vv scope, by design: everything irreplaceable gets two
offsite copies at two providers; phone video keeps relying on iCloud (a later
`immy backup coverage` step finds the dated videos iCloud does *not* have).
Measured 2026-10-12: 1,949 GiB / 150,796 files of originals in scope.

## Layout on Drive

```
Immich-DR/
  originals/        plain mirror of the scope above (YYYY/MM/…, YYYY-MM-name/…)
  media/library/    plain (uploaded-asset originals) + profile/ upload/
  _private/         rclone crypt (content + names encrypted)
    db/             immich-<ts>.sql.gz (newest KEEP_DUMPS) + RESTORE-RECIPE.txt
    immy/state/  immy/sidecars/  immy/manifest/manifest.sqlite
  _history/<run>/   files that sync replaced or deleted (backup-dir), pruned after HISTORY_DAYS
  MIRROR-STATUS.txt last complete run, scope, counts (plain, human-readable)
```

**Plain vs encrypted.** Media is plain: that is what makes Finder browsing
work, and most of it is already in Google/iCloud. The DB dump is the sensitive
object (faces, GPS, captions of everything in one file) and is useless in
Finder, so it, immy state and the manifest go through an rclone `crypt` remote
layered on `Immich-DR/_private`. The crypt passwords must also live outside n5
(password manager / paper) or the encrypted half is lost with n5.

## Mechanics — `immy/deploy/n5/backup/gdrive-mirror.sh`

A sibling of `nightly-mirror.sh`, same conventions: `gdrive.env` (gitignored)
overrides defaults, `DRY_RUN=1` from the CLI always wins, `flock`, per-run
log, Healthchecks `/start` + success / `/fail`, transient ZFS snapshots torn
down on exit. Cron 08:00 daily, after the 05:00 vv run.

1. **Preflight**: rclone present, both remotes configured, datasets exist,
   Drive free space ≥ `MIN_DRIVE_FREE_GB` (`rclone about`), the newest dump in
   `DUMP_DIR` is younger than `MAX_DUMP_AGE_H` (the vv job makes it; this job
   never dumps again), manifest stage copy present.
2. **Snapshot** `tank/immich/originals`, `tank/immich/media`, `flash`.
3. **Empty-source guard**: count files in each snapshot view (guards-lib
   `count_files`, fail closed); refuse to sync an empty view.
4. **Files first**: `rclone sync` originals and `media/{library,profile,upload}`
   from the snapshot views, with the shared scope filter, `--backup-dir
   Immich-DR/_history/<run>/…` (a deletion or replacement is a server-side move
   into history, not a loss), `--max-delete`, `--max-transfer` /
   `--cutoff-mode cautious` and `--drive-stop-on-upload-limit` (the 750 GB/day
   cap), `--fast-list`, `--tpslimit`. Size+modtime comparison daily; hashes on
   the monthly check.
5. **Then private**: `rclone copy` the newest dumps + recipe to `crypt:db/`
   (prune to `KEEP_DUMPS` there), `sync` immy state + sidecars from the flash
   snapshot, copy the staged `manifest.sqlite`.
6. **Prune history** older than `HISTORY_DAYS` (permanent delete, so quota is
   actually freed).
7. **Status**: write `MIRROR-STATUS.txt`; exit codes — a run cut short by the
   transfer cap (rclone exit 8 / upload-limit) is a *partial* success: logged
   as `SEEDING`, success ping, next run resumes. Anything else fails the run.
8. **Monthly** (`VERIFY=1`, cron on the 1st): `rclone check --one-way` with
   the same filter (MD5 vs local hash; reads the whole local scope) plus
   `--download` on one sampled month.

**Shared scope.** The dated-video and proxy excludes move into `guards-lib.sh`
as one list rendered two ways — rsync `--exclude` args for vv, an rclone
filter file for Drive — so the two mirrors cannot drift apart.

**First seed**: ~1.95 TiB at ≤ ~700 GB/run ⇒ 3–4 daily runs. Drive for
Desktop is installed on the Mac only after a clean `VERIFY=1` run, in
streaming mode, never "available offline", never saving into the tree (rclone
on n5 is the only writer; a Finder deletion is restored by the next sync).

**OAuth client**: rclone's shared client ID works but is rate-limited; a
personal Google Cloud OAuth client is recommended (one-time, user action).

## Out of scope (later phases)

- `immy backup coverage`: per-asset copy count (n5, vv, Drive, Photos.app /
  iCloud) and `--emit-filter` for a "dated video not in iCloud" tier.
- `immy export views`: album / trip / person folders as Drive shortcuts over
  the single plain copy.
- Self-describing mirror: `immy snapshot` catalog beside the files.

## Restore drill (before trusting it)

Pull one month of originals and the newest dump + recipe from Drive into a
scratch dir, restore into a throwaway Postgres at the recipe's image tag,
check an album, a named person and a CLIP search; document the path remap.

## Testing

- `tests/test-guards.sh` grows cases for the shared scope renderer (rsync and
  rclone forms agree on a fixture tree: `rclone lsf --filter-from` vs
  `rsync --dry-run` lists).
- `DRY_RUN=1` run on n5 against the real remote before the first live run.
