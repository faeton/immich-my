# Audit fix plan — 2026-10

Source: audit list (2026-09-30) re-verified against HEAD 600caea by four
verification agents + a Codex review. Findings below are the verified ones;
file:line refs are as of 600caea (re-locate if drifted).

## Global Constraints

- Repo root `/mnt/flash/immy/immich-my`, package in `immy/`. Run tests from
  `immy/`: `uv run --no-sync pytest -q -p no:sugar` (baseline 802 passed, 3 skipped).
- AGENTS.md rules bind: originals are immutable (metadata → XMP sidecars / DB);
  keep the Mac path byte-identical when adding NAS behaviour (new config
  defaults to the old path); work and commit on `main` directly; commit
  messages with backticks via `git commit -F <file>`.
- Target is Immich **3.0.2** (live DB). Live Postgres is reachable read-only at
  127.0.0.1:15432 using `/mnt/flash/immy/config.yml` with host/port overridden
  (`dataclasses.replace(cfg.pg, host='127.0.0.1', port=15432)`). **Read-only
  queries only — never write to the live DB**, never rebuild/restart docker,
  never push.
- Every bug fix gets a regression test that fails before the fix.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Add a CHANGELOG.md entry per task (newest-first, match existing style).

## Task 1: Schema contract + fix current SQL against 3.0.2

Live bugs: process.py:322 and offline.py:179 INSERT `"deviceAssetId"`,
`"deviceId"` — columns absent from 3.0.2 `asset`. `asset.duration` is int4
(milliseconds) but immy writes `"HH:MM:SS.sss"` strings (process.py:236-242,
video.py:157-165, `update_asset_duration` process.py:937, offline replay
offline.py:845). docs/IMMICH-INGEST.md:57 still says varchar.

- Commit a schema snapshot of 3.0.2 for every table immy writes (asset,
  asset_exif, asset_file, asset_face, face_search, smart_search, album*,
  tag*, stack, etc. — find them by grepping INSERT/UPDATE/DELETE in src):
  table, column, data_type, is_nullable, column_default. Prefer
  `pg_dump --schema-only` if reachable; otherwise generate from
  information_schema via the read-only connection, plus a small regen script.
- A contract test that extracts column lists from every INSERT/UPDATE SQL
  literal in src/immy and asserts each column exists in the snapshot, and
  that NOT NULL columns without default are supplied by every INSERT.
- Fix the INSERTs (drop deviceAssetId/deviceId; supply anything 3.0.2
  requires). Duration → integer ms everywhere it is written; legacy
  `H:MM:SS(.fff)` strings (offline cache replay, journal) are normalised to ms.
- Runtime guard: before process/promote write anything, check the live
  columns (reuse doctor's `_check_columns`, but drive its list from the same
  contract source so they cannot drift); abort with a clear message on
  mismatch. Doctor gains the full write-target coverage.
- Update docs/IMMICH-INGEST.md duration/schema notes.

## Task 2: Transaction + journal atomicity (audit 5, 6)

- process.py enricher writes (derivatives dims/duration ~913-959, CLIP ~997,
  faces ~1033, transcript ~1069, caption ~1244) swallow SQL errors with no
  savepoint; the Postgres txn aborts and `sink.commit()` (~1298) silently
  rolls back the whole asset. `mark_done` runs before commit, so the journal
  says done for rolled-back work (e.g. CLIP cached, smart_search row never
  exists). The commit-failure branch even flushes the journal.
- Fix: per-enricher savepoint (`with conn.transaction():`) so one failure
  doesn't poison the asset; journal marks for an asset become durable only
  after its commit succeeds; before commit, if
  `conn.info.transaction_status` is INERROR, raise (fail loud).
- process.py:742-749 overwrites the DB-resolved asset id (set by
  `PgSink.insert_asset_and_exif`, offline.py:281-303) with the journal's
  `ingest.meta.asset_id`. DB id must win; journal id used only when no DB row
  exists; on mismatch warn and rewrite the journal entry.
- Also: `is_done` for later phases must not treat a phase as cached when the
  asset row was (re)inserted in this run.

## Task 3: Face-person links, promote scope, resurrect status

- `pg.replace_asset_faces` (pg.py:134-185) deletes all `machine-learning`
  faces for an asset — including those with `personId` (68,919 of 93,470 in
  the live DB) — and re-inserts unassigned. Fix: never delete faces that
  have a personId (or carry personId over by IoU match to new boxes); only
  replace unassigned ML faces. Test both.
- promote.py ~781 and related: trip paths go into `LIKE` unescaped (`_`,`%`
  wildcards hit other trips) and NULL libraryId rows are admitted. Escape
  (`ESCAPE '\'`) and scope strictly to the library.
- `--resurrect-deleted` (promote.py:785-793) and the default un-offline
  branch (810-816) clear deletedAt but leave `status='trashed'`
  (`assets_status_enum {active,trashed,deleted}`). Set `status='active'` for
  trashed rows; include `status='trashed'` in WHERE/counts (831-864); leave
  `status='deleted'` alone.

## Task 4: Clock-drift rules

- rules/clock_drift.py:59-81 flags anything >24h from the folder median and
  patches it to the single median timestamp; a 10-day single-camera trip has
  8/10 files flagged and `--yes-medium` (cli.py:310) collapses them.
- rules/clock_drift_by_camera.py:177-185 compares per-camera medians; a
  drone flown only on day 9 is told "+83h".
- Fix: drift is only inferred from temporally overlapping evidence
  (nearest-neighbour deltas between cameras within the same session); no
  overlap → no proposal. Proposals are deltas preserving spacing, never a
  constant. Single-camera folder rule must not flag a continuous multi-day
  trip (use session gaps, not distance from global median). Cap
  uncorroborated deltas (~26h). Regression tests for both repros.

## Task 5: Date/time and sidecar precedence

- localDateTime written as UTC (process.py:232-234 via `_to_utc`). Reuse
  `backfill_dates._compute_instant` (117-146) in row building: localDateTime
  = naive wall time; dateTimeOriginal/fileCreatedAt = true UTC instant;
  QuickTime CreateDate is UTC.
- Sidecar fixes lose to embedded values: `_best_datetime` (process.py:122-133)
  prefers EXIF over XMP DTO; GPS (307-312) prefers Composite/EXIF over XMP;
  tz (314) ignores XMP offset. XMP written by immy must take precedence for
  the fields immy writes.
- NAS: `exif.read_folder` (exif.py:111) only finds `f.with_suffix('.xmp')`;
  sidecars under `sidecars_root` are never read. Resolve via
  `WritablePaths.xmp_path` (Mac path unchanged).
- Takeout rescue: dedup/engine.py:192 `utcfromtimestamp` → naive UTC written
  as naive DTO in `_rescue_sidecar` (~1446) → shifted by UTC offset. Write
  with explicit +00:00 offset (DTO + OffsetTimeOriginal).

## Task 6: Dedup metadata + apply gating (audit 2, 3)

- dedup/engine.py:263-265 uses `-fast2`, which drops MakerNotes (BurstUUID,
  ContentIdentifier, AdjustmentVersion) and QuickTime tags on some MOVs.
  Switch to `-fast`; same in exif.py:119 `read_folder` (affects bloat scoring
  too). Provide a way to re-fingerprint existing manifest rows' burst/live
  fields (command or migration), and a test using a real HEIC fixture if
  one is in tests/fixtures (else construct via exiftool).
- `apply_decisions` (engine.py:1537-1627) processes rows by id; losers can
  be quarantined while the winner's move failed. Process per cluster,
  winner first; quarantine losers only if winner is promoted/canonical with
  verified dest+sha256; else skip and record. Quarantine purge must refuse
  rows whose cluster winner isn't promoted/canonical.

## Task 7: Atomic snapshot + backups

- snapshot.py:241-242 unlinks the old snapshot before fetching. Write to a
  temp file, then `os.replace`. Readers (duplicates.py:318 etc.) require a
  completeness marker / meta asset_count.
- deploy/n5/backup/nightly-mirror.sh mirrors only originals + media. Add
  `/mnt/flash/immy/state`, `/mnt/flash/immy/sidecars`, and the dedup
  manifest (`/mnt/tank/media/state/manifest.sqlite`, via `sqlite3 .backup`
  to a temp copy). Script change only — do not run it.

## Task 8: Offline NAS path, exit codes, marker provenance, promote --verify

- offline.py `iter_entries` (726-727) and `_replay_entry` (909) hard-wire
  `<trip>/.audit/offline`; NAS writes to `paths.offline_dir`. Thread
  WritablePaths through; callers cli.py:1864,1907, promote.py:524,564.
- Exit codes: process batch Ctrl-C exits 0 (cli.py:1817-1838) → 130.
  Promote step errors (scan, derivatives, album, stack, tags, offline sync,
  reembed; cli.py:609-690) and repair-thumbs failures (cli.py:2386-2415,
  repair.py:236-238) → exit 1.
- y_processed marker (process.py:1636-1691): add db identity
  {host, database, library_id}, mode (online/offline), enabled steps +
  versions. `is_trip_fully_cached` and promote compare these against the
  current config; mismatch → not cached.
- `immy promote --verify`: compare Immich album asset count with local
  file count for the trip; non-zero exit on mismatch.

## Task 9: Bloat, telemetry

- bloat: `_is_insta360` (rules/bloat_candidate.py:60-67) ignores 2:1
  equirectangular ≥5760 wide and `Incoming360` folders → treat as Insta360.
  Transcode temp `X.optimized.mp4.part` (bloat.py:455-462) fails in ffmpeg:
  pass `-f mp4`/`-f mov` or name `X.optimized.part.mp4`.
- srt.py: accept `longtitude`; old DJI format (lon,lat order, integer
  fields, optional `M`, dotted dates `2017.08.19`).
- rules/dji_srt.py:54-55: below-sea-level altitude must be written so
  exiftool stores Ref=1 (`GPSAltitudeRef#=1` or "Below Sea Level", abs alt).
- GPX: track.py writes local wall time with fake `Z`; geotag_from_gpx.py:137
  picks up immy's own GPX. Mark immy GPX (creator attr) and skip them, and/or
  write true UTC.

## Task 10: Captions + CLIP guard

- captions.py: retry with exponential backoff on URLError/timeout/429/5xx;
  strip `<think>…</think>`, reject refusals/empty; include a short hash of
  prompt+max_tokens+extra_body in `caption_version` and cached meta so a
  prompt change re-captions.
- CLIP: process path upserts mlx vectors into smart_search with only a dim
  check. Read Immich's configured CLIP model (system_metadata
  `system-config` → machineLearning.clip.modelName) and refuse writes on
  mismatch; refuse mlx for smart_search unless explicitly forced. Do not
  change the default backend in config without measurement.

## Task 11: Security

- `dedup review-server`, triage, pano-server default `--host 0.0.0.0`
  with no auth (cli.py:3485, 3870, 3910). Default 127.0.0.1; update docs/
  compose to pass 0.0.0.0 explicitly where docker needs it.
- immich.py:108-120 puts `x-api-key` in ssh argv and remote curl argv.
  Pass the header via stdin (`curl -K -` / `-H @-`).

## Task 12: Docs + lint baseline

- Docs drift: DEPLOY.md / ARCHITECTURE.md / README Synology → n5 TrueNAS;
  IMMICH-INGEST.md 2.7.5 → 3.0.2; README "Known gaps" remove shipped items
  (similar, apple-people --apply, doctor, status).
- Add `[tool.ruff]` (pyflakes F + E9 only to start) to pyproject, add ruff
  to dev group, fix findings (e.g. transcripts.py:373 missing Callable).
- Out of scope this plan: splitting cli.py, dependency extras, Dockerfile
  lock-file use, CI workflow (no remote CI wanted without asking).
