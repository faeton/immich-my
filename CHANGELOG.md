# Changelog

Notable changes and findings, newest first. Format is loosely
[Keep a Changelog](https://keepachangelog.com); this project ships
continuously, so entries are dated rather than versioned.

## 2026-10-02 — final-review fixes (backup state, drift dates, dedup holds, markers)

### Fixed

- **Backup: immy state was never mirrored.** `nightly-mirror.sh` excluded
  `.audit/` for every rsync, but all per-trip state lives under
  `state/<trip>/.audit/`, so vv got nothing; the empty-source guard counted the
  excluded files and passed. Excludes are now per tree (`mirror_exclude_dirs`
  in `guards-lib.sh`): `.audit/` for originals only, `.rsync-partial/`
  everywhere, and the guard counts with the same list. The first live run
  copies the whole state tree (~9 GB).
- **`clock-drift-by-camera` compared the wrong clocks.** It read naive wall
  clocks while ingest stores instants (QuickTime CreateDate = UTC except
  Insta360, offsets honoured, sidecar first). A UTC drone beside a phone in
  UTC+2 looked like a -2 h drift, and the MEDIUM patch then shifted the
  stored instant. Capture-time resolution now lives in `capture.py`
  (`capture_time`), used by ingest, the drift rule and `backfill-dates`; the
  rule's patch carries its offset inline so ingest reads back exactly the
  corrected instant. `dates.resolve` (other rules) reads the separate `.xmp`
  sidecar first, as ingest does.
- **`backfill-dates`** reads sidecars through `sidecars_root` (NAS), puts a
  sidecar correction ahead of SRT/embedded dates, lets a file's own offset
  beat the inferred clip/trip zones (an explicit `--timezone` still wins for
  the display zone; the instant stays the file's), and `--retime` keeps the stored
  `timeZone` when none is known. The dead `-fast2` QuickTime re-read is gone.
- **`dedup apply` holds losers of needs-review clusters** (unowned `auto`
  clusters where a burst / Live / edited guard now fires), counted in
  `losers_held` with the reason.
- **Dedup recovery never accepts a destination by size alone.** A legacy
  `decided` winner whose source is gone and which has no recorded sha256 is
  no longer "found" at a same-size library file (that recorded a stranger's
  hash and released the losers); the move fails loudly and its losers are
  held.
- **Markers no longer claim unfinished work.** A step that failed for any
  asset (caption retries exhausted, an enricher savepoint rolled back) is left
  out of `y_processed.yml`, so the next run re-walks the trip and retries it
  instead of printing `[cached]`. `--recaption` bypasses the cached-trip skip
  without `--force`.
- **Withheld offline CLIP vectors can be rewritten.** When `sync-offline`
  (or promote's drain) withholds a CLIP payload, it clears that asset's `clip`
  journal entry and drops `clip` from the marker, so a normal `immy process`
  recomputes it; `process --force` also recomputes CLIP despite the journal.
- **A failed read of Immich's CLIP model no longer aborts the connection**
  (it runs in its own transaction block / savepoint).

### Changed

- **Marker DB identity is `(database, library_id)`.** host/port are still
  recorded but only informational: the same DB seen from the Mac
  (`<n5>:15432`) and the container (`database:5432`) no longer forces a rescan
  or a promote library-scan.
- `process.is_processed` (no production caller) removed.
- Docs: `ARCHITECTURE.md` storage layout follows `deploy/n5/compose.yaml`;
  `TESTING.md` Y.1/Y.3 hardware smokes are marked as DS923+ (historical).
- Tests: an autouse guard makes any socket connect to 5432/15432/2283 and any
  real `psycopg.connect` fail the test (one test reached the live port).

## 2026-10-02 — docs drift + ruff baseline

### Changed

- Docs now describe the live deployment: **n5 (TrueNAS SCALE), Immich 3.0.2**.
  `DEPLOY.md` carries a current-deployment banner and marks the DS923+ layout as
  history; `ARCHITECTURE.md`, `README.md`, `TESTING.md`, `SIDECAR.md`,
  `LANDSCAPE.md` no longer call the Synology the primary (it remains the **vv**
  cold-backup target). `IMMICH-INGEST.md` is retitled as a v2.7.5 schema reading
  with 3.0.2 deltas called out. README "Known gaps" drops shipped items
  (`similar`, `apple-people --apply`, `doctor`, `status`).
- **ruff** added to the dev group with a `[tool.ruff.lint]` baseline of
  pyflakes `F` + `E9`. Findings fixed in `src/` and `tests/` with no behaviour
  change: unused imports/locals, placeholder-less f-strings, and the genuine
  `F821` in `transcripts.py` (`Callable` was never imported; annotation-only, so
  harmless at runtime thanks to `from __future__ import annotations`).
  `process.pg_mod` is kept (`# noqa: F401`) because tests patch it by dotted path.
  `uv.lock` was updated with `--no-sync` (the `insightface` build fails on n5).

## 2026-10-02 — security: servers bind loopback, API key off the command line

### Security

- **`dedup review-server`, `triage review-server`, `pano-server` now default
  to `--host 127.0.0.1`** (was `0.0.0.0`, with no authentication). A
  non-loopback `--host` prints a one-line "no authentication" warning.
  Inside docker pass `--host 0.0.0.0` explicitly so `--publish` can reach the
  server (docstrings, `DEDUP-REVIEW-TOOL.md`, `TRIAGE.md` updated). Nothing in
  `deploy/n5/compose.yaml` or `run-batch.sh` starts these servers.
  **Action needed:** the deployed compose/launch commands on n5 (a copy
  outside this repo) must add `--host 0.0.0.0`, or the published ports stop
  answering.
- **The ssh+curl Immich transport no longer puts `x-api-key` in any argv**
  (local `ssh` or remote `curl`, both visible in `ps`). curl now runs with
  `-K -` and the key header (plus the JSON body, since stdin can be read only
  once) is written to stdin as a curl config with quotes/backslashes escaped.

## 2026-10-02 — caption retry/sanitising/prompt hash, CLIP model guard

### Fixed

- **Captions retry transient failures.** Up to 4 attempts with exponential
  backoff + jitter (1 s base, 30 s cap) on connection errors, timeouts, HTTP
  429 and 5xx; `Retry-After` is honoured. Other 4xx fail at once (the
  "invalid image" 400 re-encode path is unchanged).
- **Captions are sanitised.** `<think>...</think>` (and an unclosed leading
  `<think>`) is stripped; surrounding quotes/whitespace trimmed. Empty text,
  fewer than 3 words, and refusals ("I'm sorry", "I cannot", "I can't",
  "As an AI") are rejected: counted as a caption failure, nothing written,
  journal not marked.
- **A prompt change now re-captions.** The caption journal version is
  `caption:<model>@<hash8>` (sha256 of prompt + max_tokens + canonical
  `extra_body`), and the hash is stored in the cached caption meta. A cached
  caption made under a different hash is redone even when its DB description
  is AI-prefixed. Existing entries with no hash (`caption:<model>`) count as
  matching, so upgrading does not mass re-caption; `--recaption` is still the
  explicit override. Trip markers record the new version string, so a
  previously processed trip re-runs once (journal-cached, no VLM calls) to
  re-stamp its marker.
- **CLIP writes to `smart_search` are guarded.** immy reads Immich's
  configured model (`system_metadata` `system-config` ->
  `machineLearning.clip.modelName`, default `ViT-B-32__openai` as in Immich
  3.0.2 when unset; read-only) and refuses CLIP writes on mismatch with
  `ml.clip_model`; other enrichers continue. The `mlx` backend (~0.925 cosine
  to Immich's vectors) is refused unless `--allow-mlx-clip` or
  `ml.allow_mlx_clip: true` is given. The default backend is unchanged, so a
  Mac run on `mlx` now needs the flag or config key to keep writing CLIP.
  `process_trip` itself also defaults to refusing mlx. Offline caches record
  model/backend with each CLIP vector; `immy sync` re-applies the guard and
  withholds (and counts) vectors that mismatch, were mlx without consent, or
  predate provenance (legacy payloads), while syncing the rest of the row.
- **Retry-After** accepts HTTP-dates, is never shortened, and a request over
  120 s fails the caption instead of retrying early. An offline caption cached
  under a different prompt hash is regenerated.

## 2026-10-02 — bloat/Insta360 detection, transcode temp name, SRT dialects, altitude sign, immy GPX

### Fixed

- **Bloat rule treated Insta360 exports as deliveries.** Equirectangular 2:1
  clips >=5760 wide, anything under a path segment containing `360`
  (`Incoming360`), and Insta360 filenames are now never bloat candidates.
- **Transcode failed in ffmpeg.** The temp name `X.optimized.mp4.part` hid the
  container; it is now `X.optimized.part.mp4`. The encoder falls back to
  `libx265` when `hevc_videotoolbox` is not in `ffmpeg -encoders` (Linux/NAS);
  the Mac path is unchanged.
- **SRT parser.** Accepts DJI's `longtitude` misspelling and the old
  `GPS(..)` form in both published orders, decided once per file: an `M`-suffixed
  third field means lat-first (Matrice style); else agreement (~1 degree) with
  labelled lat/lon pairs or `HOME(..)` points elsewhere in the file; else the
  single order in range for every GPS cue; else no fix. HOME alone is not a
  dialect signature. No altitude is taken from `GPS()` or
  `BAROMETER` (vertical reference unverified). Dates like `2017.8.5` parse.
- **Below-sea-level altitude** from SRT was written as `GPSAltitudeRef=1`, which
  exiftool stores as 0 in XMP (read back positive). Now `abs(alt)` +
  `Below Sea Level`.
- **immy's own GPX was fed back into `geotag-from-gpx`** (naive local time with
  a fake `Z`). immy GPX (`creator="immy"`) is skipped; GPX time encoding is
  unchanged.

## 2026-10-02 — offline NAS drain, honest exit codes, marker provenance, `promote --verify`

### Fixed

- **Offline cache on the NAS was never drained.** `process --offline` spools
  to `WritablePaths.offline_dir` (under `state_root` on the NAS), but
  `iter_entries` / `sync_trip` / `_replay_entry` hard-wired
  `<trip>/.audit/offline`, so `sync-offline` and promote's drain found nothing
  (and replay read CLIP/faces files from the wrong root). They now take
  `offline_root`; `sync-offline` and `promote` resolve it from config. The Mac
  path (no `state_root`) is unchanged.
- **Multi-trip online `process` batches failed after the first trip.** The
  batch opens one Postgres connection, but each trip's `PgSink.close()` closed
  it. `PgSink` no longer closes a connection it does not own; the batch closes
  it once at the end.
- **Exit codes.** `process` exits 130 on Ctrl-C (was 0). `promote` exits 1 when
  any step failed (offline-cache sync, library scan, derivatives push, Insta360
  stack, album sync, thumbnail repair, tags, re-embed queueing), after running
  every step that can still run; output text is unchanged apart from a new
  "thumbnail repair failed" line. `repair-thumbs` exits 1 when a trip errors
  (including partial derivative-generation failures, a missing folder, or a DB
  error, which no longer aborts the remaining trips).

### Changed

- **`y_processed.yml` records provenance**: `db: {host, port, database,
  library_id}`, `mode: online|offline` and `steps: {name: version}` for the
  enabled enrichers (journal version strings). The cached-trip skip in
  `process` requires the same DB and mode and every requested step at the same
  version. **Markers written before this change carry no provenance, so the
  first `process` run after upgrading re-scans every trip once** (the journal
  keeps it cheap: finished work is skipped). `promote` treats a marker whose
  `db` differs from the current config as "not processed for this DB": it
  warns and takes the library-scan path instead of skipping it (every identity
  field known on both sides is compared, so a config without `pg:` still
  rejects a marker for another `library_id`). Markers without a `db` block are
  still trusted by `promote`. The offline library fallback
  (`derive_container_root_from_marker`) reads markers under `state_root` too.

### Added

- **`immy promote --verify <trip>`** — read-only check: the trip's Immich album
  (members under this trip's path; `--into-album` picks the album) vs the local
  media files `process` ingests, plus local files Immich holds as visible
  assets, minus Live-photo motion halves Immich keeps `visibility='hidden'`
  (Immich 3.0.2 never puts hidden assets in albums). Prints both counts; on
  mismatch (or a missing album) lists up to 20 missing/extra names and exits 1.
  No rsync, scan or writes.

## 2026-10-02 — atomic snapshot, backups cover immy state

### Fixed

- **`immy snapshot` unlinked the old snapshot before fetching**, so an
  interrupted run (Postgres drop, Ctrl-C) left a truncated or missing file
  that `find-duplicates` / `match` then trusted. It now builds
  `<out>.tmp` beside the target and `os.replace`s it only after the rows,
  albums and meta are written; a failed run removes the temp file and keeps
  the previous snapshot. `write_meta` also sets a `complete` marker.
- **Readers refuse an incomplete snapshot.** `snapshot.open_for_read` (used by
  `find-duplicates`, `match`, `apple-people`) raises `IncompleteSnapshotError`
  (CLI: exit 2, "re-run `immy snapshot`") when meta `asset_count` is missing or
  differs from the row count, or the marker is unset. Snapshots written before
  the marker existed are accepted only if `asset_count` matches.
- **Backups missed immy's own state.** `deploy/n5/backup/nightly-mirror.sh`
  now also mirrors `/mnt/flash/immy/state` and `/mnt/flash/immy/sidecars` (from
  a transient snapshot of the `flash` dataset) and the dedup
  `/mnt/tank/media/state/manifest.sqlite` (via `sqlite3 .backup` into a staging
  dir with an integrity check, never a raw copy) to vv `immy/`. Script change
  only: not run, and the installed copy under `/mnt/tank/scripts/` is unchanged.
  See `deploy/n5/backup/README.md`.
- **Backup guards (review round 1).** The mirror fails closed before any
  `--delete` transfer of state/sidecars when the snapshot view is empty (unless
  `ALLOW_EMPTY_SOURCES=1`); the manifest source is opened `-readonly` and the
  copy must be non-zero, have `asset`/`cluster` tables and >= `MANIFEST_MIN_ROWS`
  rows. `immy snapshot` now uses a unique `mkstemp` temp file so overlapping
  runs cannot clobber each other.

## 2026-10-02 — dedup: maker notes read, losers wait for their winner

### Fixed

- **Burst and Live Photo ids were never recorded.** `dedup fingerprint`
  and `exif.read_folder` ran exiftool with `-fast2`, which skips maker
  notes — where Apple keeps `BurstUUID`, `ContentIdentifier` and
  `AdjustmentVersion` — and loses the QuickTime tags of some MOVs. On
  real iPhone HEICs in originals/2026/03, `-fast2` returned 0 of 164
  ContentIdentifiers, `-fast` all 164; every row in n5's manifest has
  NULL burst/live ids, so the burst and Live-pair guards in Stage D never
  fired. Both now use `-fast` (slower, accepted). `immy dedup fingerprint
  --refresh-meta` re-reads those ids (and `edited`) for rows already
  fingerprinted: fills NULLs only, never sizes/hashes/status, idempotent,
  reads promoted/quarantined rows at their `dest_path`. Clusters are
  reconsidered in the same transaction as each batch's metadata, so a
  killed run never leaves an id recorded and its stale merge executable.
  Only merges `decide` made are acted on (schema v5 adds
  `cluster.decided_by`: `machine` from `decide`, `human` from the review
  tool; existing clusters migrate as unknown). Such an `auto` cluster that
  gains an id and has moved nothing is reopened (`pending`, members back to
  `clustered`; re-run `dedup decide`); in one already applied the moves
  stand and members still `decided` drop to `clustered`, so promote-rest
  keeps them. A person's merge, or one of unknown provenance, is never
  changed: when a burst / Live / edited guard would now fire on it, its
  cluster id is listed for review. Rows promoted before schema v4 recorded
  no `dest_path` and are counted `missing`, not guessed.
- **`dedup apply` could quarantine a loser whose winner never reached the
  library.** Rows were walked by asset id, so a lower-id loser moved
  before its winner was even tried, and a failed winner left the shot's
  only copy in quarantine. Apply now goes cluster by cluster, winner
  first, and quarantines a loser only when the winner was promoted by this
  run or is proven in the library now: the file the manifest points at
  (dest_path; for a pre-v4 promote the expected path or its `__<id>`
  collision name, then recorded; a canonical row's own path) hashes in full
  to the sha256 recorded for it (a canonical row may use its `library_file`
  hash from `dedup index-library`). No recorded hash, a missing or changed
  file, or two matching candidates: the loser stays `decided`, is counted
  as `losers held` with its reason, and is retried on the next run.
  `engine.purge_candidates` applies the same proof to every quarantined
  row (an alias: its library twin must still hash to the alias's sha256).
  There is no purge command yet; any future purge must call it
  immediately before deleting and delete only what it returns. Pre-v4
  rows (all of n5's current quarantine) carry no sha256, so they are
  refused until a hash can be established.

## 2026-10-02 — capture time: wall-clock localDateTime, sidecars win

### Fixed

- **`immy process` stored `localDateTime` as the UTC instant and ignored
  offsets.** Immich 3.0.2 keeps `localDateTime` as the capture wall clock
  stored as if UTC, and `fileCreatedAt` / `dateTimeOriginal` as the true
  instant (checked against live rows: a Kyiv 15:20 shot is
  `localDateTime 15:20+00`, `dateTimeOriginal 12:20+00`). Ingest now
  applies the offset that belongs to the chosen date (`OffsetTimeOriginal`
  for EXIF, the inline offset for XMP, `QuickTime:TimeZone` for
  QuickTime — a losing tag never lends its offset), prefers an Apple
  `CreationDate` carrying an offset, and writes the wall clock to
  `localDateTime` and the instant to the other two. `QuickTime:CreateDate`
  is UTC except for makers in `QUICKTIME_LOCAL_CLOCK_MAKES` (Insta360,
  verified on real `.insv`/GO 2 files to record local wall time; DJI and
  GoPro checked and do write UTC).
  With no offset or zone known, the naive time is still taken as UTC for
  both. Offsets go to `asset_exif.timeZone` in Immich's own `UTC+2`
  spelling instead of a bare `+02:00`.
- **Sidecar fixes lost to embedded tags.** A date, GPS fix or offset in a
  separate `.xmp` sidecar (immy's audit rules or the user) now beats the
  file's embedded EXIF/Composite values at ingest. XMP embedded in the
  file is not elevated over embedded EXIF. `exif.read_folder` keeps the
  sidecar's tags apart (`ExifRow.sidecar`) so this precedence doesn't rely
  on the `XMP:` group alone.
- **NAS sidecars were never read.** `read_folder` only looked for the
  sibling `<stem>.xmp`; with `sidecars_root` set, `immy process` now reads
  sidecars from `WritablePaths.xmp_path` (where it writes them). The Mac
  layout is unchanged.
- **Takeout date rescue shifted by the UTC offset.** Google's
  `photoTakenTime` is a UTC epoch but was written to the XMP sidecar as a
  naive `DateTimeOriginal`; it now carries an explicit `+00:00`. The same
  write-back stored `abs()` GPS with a separate `GPS*Ref`, which exiftool
  ignores for XMP — western/southern fixes landed in the wrong hemisphere;
  coordinates are now written signed.
- **`backfill-dates` wrote `localDateTime` as a naive timestamp**, which
  Postgres read in the Immich DB session zone (Europe/Lisbon), shifting it
  by an hour in summer. It is now sent tagged as UTC.

Assets already in the DB are not migrated: `immy backfill-dates --retime
<trip>` (review the dry-run, then add `--apply`) recomputes their dates,
with limits: it reads only sibling `.xmp` sidecars (not `sidecars_root`
on the NAS), and a clip/trip zone it finds (or none → NULL) replaces the
file's own offset in `timeZone` and in the localDateTime it derives.

## 2026-10-02 — clock-drift: evidence-based deltas, never a median

### Fixed

- **`clock-drift` collapsed multi-day trips onto one instant.** It flagged
  every file >24 h from the folder median and patched each to that single
  median timestamp, so a 10-day single-phone trip had 8/10 files flagged
  and `--yes-medium` rewrote them all to the same moment. The rule now
  splits the folder into sessions (no gap >3 h) and flags only files whose
  session is >24 h from every other session, isn't the trip's biggest
  session, and holds ≤25 % of the files. Its proposal is a delta (a
  whole-year or whole-hour shift that uniquely lands the file in a
  session), never a constant. One clock can't corroborate its own shift
  (a genuine next-day photo 25 h later "lands" too), so every finding is
  LOW — reported, never auto-applied — and with no clean shift it is a
  LOW note with no patch.
- **`clock-drift-by-camera` inferred drift from medians.** Comparing
  per-camera median times told a drone flown only on day 9 of a 10-day
  trip that it was "+83h" off. Drift is now found by offset search:
  bursts (≤60 s) collapse to independent events, candidates are whole
  hours ±14 h plus ±5 min skew, each scored by one-to-one event matches
  within 2 min. The best hour-peak needs ≥3 matched events, ≥30 %
  coverage of the smaller camera's total events, and must beat the
  runner-up (zero offset included) by ≥2 and ≥2×; the offset is refined
  by the median matched delta. A +3h time-zone slip is found; one
  correctly timed drone burst is not drift; ambiguous → no proposal.

## 2026-10-02 — face→person links, promote path scope, trash status

### Fixed

- **Re-running faces orphaned people.** `replace_asset_faces` deleted every
  `machine-learning` face on the asset — including the ones Immich had linked
  to a person (68,919 of 93,470 in the live library) — and re-inserted them
  unassigned. Faces with a `personId` are now never deleted; only unassigned
  ML faces are replaced, and a new detection overlapping a kept person face
  (IoU ≥ 0.5, normalized coords) is skipped so the person keeps exactly one
  face there.
- **`promote` album sync / `repair-thumbs` matched other trips.** The trip
  path went into `LIKE` unescaped, so `_`/`%` in a folder name were
  wildcards (`2024_06-x` also hit `2024-06-x`), and rows with a NULL
  `libraryId` were admitted. The prefix is now escaped (`pg.like_prefix`,
  `ESCAPE '\'`) and every query is scoped to the configured library only.
- **Un-trashed assets stayed hidden.** Immich 3.x tracks trash in
  `asset.status` (`active|trashed|deleted`) as well as `deletedAt`; promote's
  un-offline / `--resurrect-deleted` UPDATEs cleared only `deletedAt`, leaving
  `status='trashed'`. They now set `status='active'`, never touch rows already
  `deleted` (pending hard-delete), and the album list / skipped-trash count
  account for `status`.

## 2026-10-02 — `process`: transaction + journal atomicity

### Fixed

- **One failed enricher statement silently dropped the whole asset.** The
  derivatives (dims/duration), CLIP, faces, transcript and caption writes
  swallowed SQL errors with no savepoint, so Postgres aborted the asset's
  transaction and the per-asset COMMIT quietly turned into a ROLLBACK. Each
  enricher now runs in its own savepoint (`Sink.savepoint()`), so a failure
  costs only that phase. As a backstop, `PgSink.commit` refuses to COMMIT an
  aborted transaction (`TransactionAborted`): the asset is reported failed and
  the trip exits non-zero.
- **The journal claimed work that was rolled back.** Phases were marked done
  before the commit, and the commit-failure branch even flushed them, so
  e.g. "CLIP done" stuck for assets with no `smart_search` row. Journal marks
  are now staged (`Journal.stage` / `commit_staged` / `discard_staged`) and
  become durable only after the asset's commit succeeds (offline: after the
  cache entry is on disk). Applies to the parallel caption pool too.
- **A stale journal asset id overrode the DB's.** On an `ON CONFLICT` resume
  the id resolved from the DB now wins; the journal's id is used only when no
  row resolves, and a mismatch is warned about and rewritten.
- **Cached phases were trusted for a re-inserted row.** If this run inserted
  the asset row (or its id changed), the journal's derivatives/CLIP/faces/
  transcript/caption entries describe a row that is gone; they are now cleared
  and the phases re-run.

## 2026-10-02 — Immich 3.0.2 schema contract

### Fixed

- **`process` / `sync-offline` INSERTs failed on Immich 3.0.2.** The `asset`
  INSERT wrote `"deviceAssetId"` and `"deviceId"`, both dropped in 3.x; every
  new asset errored. Removed from the INSERT and from `AssetRow`.
- **`asset.duration` is `int4` milliseconds in 3.x**, but immy wrote
  `"HH:MM:SS.sss"` strings (initial INSERT, ffprobe UPDATE, offline replay).
  It now writes integer ms everywhere (`video.duration_ms`); offline-cache
  entries written before this fix are normalised on replay
  (`video.normalize_duration_ms`).

### Added

- **Schema contract.** `immy/src/immy/data/immich_schema.json` snapshots every
  table immy writes as 3.0.2 ships it (from information_schema;
  regenerate with `immy/scripts/regen_immich_schema.py`).
  `tests/test_schema_contract.py` extracts every INSERT/UPDATE from the source
  and checks each column exists, that each INSERT supplies every NOT NULL
  column without a default, and that `schema_contract.WRITE_COLUMNS` is exactly
  what the source writes.
- **Pre-write guard.** `process`, `promote` and `sync-offline` check the live
  columns against that contract (missing, retyped, or newly required) and exit
  2 with the list before writing anything; `promote` also re-checks on each of
  its write connections, so a preflight that couldn't reach the DB never lets a
  later step write unchecked. `doctor` now reports the same check
  for all seven write tables (it used to check a hand-written subset).
- `docs/IMMICH-INGEST.md` notes the 3.0.2 differences.
## 2026-09-30 — `clock-drift` no longer collapses a second shooting day

### Fixed

- **`clock-drift` on multi-day trips.** The rule flagged every file >24 h from the
  folder median and proposed the median itself as the fix — the *same* instant for
  every outlier. On `2026-08-la-manga` (flights Aug 31 + Sep 4) that meant 24 Sep-4
  files, all with correct SRT-derived dates, proposed as `2026-08-31 10:50:13`.
  Timestamps are now split into sessions on >24 h gaps; a session of ≥ 3 files is a
  real day and is left alone. Lone stragglers are still flagged.

## 2026-09-23 — manifest identity (schema v4), `photos` adapter, doctor/status/prune

Phase 2 of the Photos Bridge plan (`todo/PHASE2-IDENTITY-DESIGN.md`, rev 3 after
two Codex design reviews) plus the n5-side of Phase 3 and three ROADMAP items.
None of it needs the Mac.

### Added

- **Schema v4.** `asset` gains `source_uid`, `component`, `sha256`, `dest_path`,
  `alias_path`; new `library_file` table — a content index of what the library
  actually holds. Migration is one transaction (columns + version bump), inspects
  `PRAGMA table_info` so a half-migrated or version-less manifest completes, and
  creates indexes only after it. Checked on a copy of n5's live manifest (285k
  rows): < 1 s, integrity ok.
- **Content identity at fingerprint time.** Every new arrival is hashed. A
  non-`originals` file whose exact bytes a library file holds becomes `alias`
  (no pHash, never clustered); `dedup apply` quarantines it after re-hashing
  **both** files in full, or sends it back to `registered` if the proof fails.
  `originals` rows are never aliased — bootstrap builds the index instead.
- **Stub guard**: 0 bytes, exiftool `Error`, or a media-named file whose content
  sniffs as `text/*` → `error: stub: …`. `immy dedup retry-errors [--match stub]`
  resets them once the real file arrives.
- **`immy dedup index-library --originals … [--dir 2026/05 …]`** — hash library
  subtrees into `library_file`; resumable, prunes vanished files. 2026/07
  (426 files, 9 GB) took 28 s on n5.
- **`photos` source adapter** (`dedup/photos.py`): Photos UUID from the osxphotos
  JSON export report, component (`original`/`live_video`/`raw`/`edited`) from the
  exported name, Photos-corrected date/location from the JSON sidecar
  (`taken_src='json'`, so promote writes it back as XMP). A promoted twin with a
  different hash is held as `error: revision of #N`.
- **`immy doctor`** — read-only preflight (binaries, libvips, roots, ML backend
  coherence, Immich API + import paths, Postgres, direct-write columns, CLIP dim).
- **`immy status <trip>`** — audit pending, process marker, journal per worker,
  offline synced/pending, staged derivatives, heartbeat.
- **`immy cluster --prune`** — removes stale members immy itself added, tracked
  in a ledger; hand-added photos are never touched.

### Changed

- **Moves record before they consume.** `_safe_copy` (copy → fsync → rename →
  fsync dir → full sha256) replaces `_safe_move`; `dest_path` + `sha256` are
  committed before the source is unlinked. Recovery uses the record: a source
  that changed since is refused, never consumed; a recorded file that no longer
  matches with the source gone is refused rather than size-guessed.
- `_resolve_dest` requires **full sha256** equality before claiming a
  destination as this asset's own — `content_equal`'s sampled windows can pass
  two >16 MB files differing elsewhere (a test pins that counterexample).
- `dedup apply`, `dedup promote-rest` and `triage apply` share one lock,
  `<manifest>.movers.lock`.
- Triage's in-place swap clears `asset.sha256` and the file's `library_file`
  row (it preserves mtime, so a stat check alone could miss it).
- `photos` source weight 110; `_edited` suffix counts as edited; JSON date
  rescue no longer Google-only; review UI styles `.src.photos`.

### Found

- **osxphotos' JSON sidecar has no UUID** (0.77.1 source): the review's plan to
  take identity from it would not have worked. The JSON *export report* has it.
- **Tests run on n5 now**: `uv sync --no-install-package insightface` +
  `pyvips-binary` + user-local exiftool (AGENTS.md). 802 passed.

## 2026-09-18 — dedup safety: four ways identity was being guessed

Phase 0.5 of the Photos Bridge review (`todo/PHOTOS-BRIDGE-REVIEW.md`) — live
bugs in the existing cascade, fixed ahead of any bridge work and independent
of it. All four shared one root: byte length, a filename stem or a stale
score was standing in for content identity, on paths that delete or
quarantine files.

### Fixed

- **`_resolve_dest` could delete an asset that never reached the library**
  (P0.1, the urgent one). An existing destination of the expected size was
  read as this asset's own completed move, and both callers answer that by
  unlinking the staging file *without copying it anywhere*. A library file
  of equal length at the same `YYYY/MM/basename` was enough: the asset was
  marked `promoted` and was simply gone. Destination identity now requires
  verified content equality; only a source that is already consumed (copy
  and unlink done, status commit missing) still resolves on size, and
  nothing is deleted on that path.
- **Same-stem, same-size videos auto-merged on filename alone** (P0.2).
  `_pair_evidence` returned `("strong", None)` on equal byte length *before*
  the `VIDEO_STEM_PLAUSIBILITY_SECONDS` gate — so the gate added 2026-07-12
  sat behind the very shortcut it was meant to protect, and `_decide_one`'s
  video branch tested only `bytes`, with `_metadata_agrees` falling through
  to a bare stem match. Both now confirm the bytes; an unreadable file is
  not a confirmation. Conflicting `live_cid` (Apple ContentIdentifier) is a
  hard bar on auto-merging anywhere.
- **An extended cluster kept a stale CLIP score** (P0.3). `cluster()` merges
  new members into an existing `cluster_id`, but `_clip_ready_clusters` only
  visits clusters where `clip_cos_sim IS NULL` — so a fresh arrival
  attaching to a settled cluster (which `originals` rows deliberately enable)
  inherited a cosine earned by two other images and could be auto-merged away
  on it. Membership growth now clears the score, which re-queues Stage C and
  routes the cluster to review until it is recomputed. `dedup cluster`
  reports the count.
- **RAW/JPEG companion exclusion did not survive transitivity** (P0.4). The
  exclusion suppressed only the direct RAW↔JPEG edge; union-find still joined
  both components through any third image matching each, and both — including
  the irreplaceable RAW — became losers. The check now runs over every member
  pair in `_decide_one`, where transitive clustering cannot route around it.

### Changed

- `content_equal()` is the one place content identity is decided: a full
  byte compare up to 16 MB, three sampled windows above it, capped at ~12 MB
  per side so clustering a video library does not turn into hundreds of GB
  of reads. Silent-corruption detection stays with `_safe_move`'s sha256.
- `tests/test_dedup_safety.py` — 20 tests pinning all four, including
  end-to-end `promote_rest` / `apply_decisions` runs against a same-size
  stranger sitting at the destination. Needs no pyvips, so it runs on the
  NAS too.

## 2026-09-08 — `immy similar`: image→image search

### Added

- **`immy similar <photo>`** — "which library shot is this?" Immich's UI only
  searches by text, but its `smart_search` table + vchordrq cosine index are
  queryable directly. The command embeds the query with Immich's own ONNX
  ViT-B-32 (or the NAS ML server via `--backend immich-ml`; both share the
  index's vector space — `mlx` is refused) and prints the nearest neighbours
  with date, place and a verdict. Read-only. `--json` for asset ids.

### Found

- **Calibration on the live library.** A 343-px, q65 re-compression of a
  library HEIC scored **0.989** against its original, while selfies of the
  same person from different years all cluster at **0.92–0.94**. Hence the
  verdict bands: ≥0.95 same frame, 0.85–0.95 same subject, else similar.
  Faces: the same re-compressed copy scored **0.951** against its own
  `face_search` row; every other shot of the same person peaked at 0.82.
- **CLIP coverage is image-complete.** 137k of 223k live assets have a
  vector; the 85k gap is almost entirely videos in the external library
  (83,714) plus 1,483 images. So an image miss means "not in Immich", not
  "not embedded". The command prints coverage on every run.

## 2026-07-19 — `immy triage apply` (executor, phase 3: compress)

### Added

- **`immy triage apply`** — executes pending `compress` verdicts:
  mp4→SVT-AV1 10-bit / mov→x265 10-bit (container and path never change,
  preserving Immich asset identity), duration-verified, original
  quarantined, owner/mode/mtime carried over, `exec_log` journal,
  crash-safe staged swap with `heal()`, no-gain guard, biggest-first and
  resumable. One Immich rescan per run. Smoke-verified on n5 (av1 -92%,
  hevc -62%).
- Fat-tail auto-sweep on n5: 2,047 non-360 clips >40 Mbps marked
  `compress` by `rule:fat-tail-40mbps` → queue now 2,407 clips / 836 GB.
- 360 viewer additions: raw-fisheye de-warp fallback (+front/back lens
  toggle) for recordings with no stitched preview on disk; X4/X5 naming
  (`LRV_*.lrv`, `360VID_*.mp4`); triage→viewer playback deep links.

## 2026-07-19 — `immy triage review-server` (footage triage, phase 2)

### Added

- **`immy triage review-server`** — web UI for grading trip footage
  (`triage/review.py`, port 8766): trip index sorted by undecided GB, then
  one trip per screen with clips in capture order grouped into take blocks.
  Each clip is a 6-frame contact sheet from the scan's frame cache with
  duration/size/bitrate/codec, a lightbox (frame cycling + in-browser
  playback of mp4/mov via Range requests; .insv greys out), and keyboard
  verdicts: K keep · C compress · A archive (`cold`) · T trash · U undo,
  shift+key for the whole take, H hides decided. Verdicts land in the
  `triage` table (`decided_by='human'`); rows the executor has applied
  (`applied_at` set) are locked against re-grading. Never touches a file.

### Changed

- **Suggestion rules retuned from the first real scan** (n5, 3,808 clips):
  album membership no longer suggests keep — immy's auto-albums cover every
  trip clip, so the rule blanket-kept 1.86 TB and starved the others.
  Favorites (still a keep) are genuinely rare. Compress threshold dropped
  60 → 40 Mbps: the library's H.264 averages 51 Mbps and HEVC 88 Mbps, so
  60 excluded most of the plausibly re-encodable long tail.

## 2026-07-18 — `immy triage scan|report` (footage triage, phase 1)

### Added

- **Manifest schema v3**: `triage` (human verdicts keep/compress/cold/trash,
  `applied_at` NULL until a future executor acts) and `video_signal`
  (scan-derived, rebuildable). Migration is table-creation only — existing
  v2 manifests upgrade on open.
- **`immy triage scan`** — per-clip signals for every trip video (named
  `YYYY-MM-*` dirs only; the dated cloud tree is out of scope): ffprobe
  duration/codec/bitrate, 6 sampled frames, a pooled clip-level CLIP vector
  (cached in `embedding`), Immich favorite/album flags read directly from
  PG (`album_asset`), take-grouping (>120 s gap or centroid cosine <0.80
  starts a new take), and conservative advisory suggestions. Resumable and
  ^C-safe; derived layers recompute every run so rule tweaks need no
  `--force`. Runs in the deploy/n5 container (ffmpeg + immich-ml + PG live
  there); see `immy/TRIAGE.md`.
- **`immy triage report`** — per-trip rollup (clips, GB, take-group GB,
  compress-candidate GB, favorites), biggest first; `--json` for machines.
  Never touches a file — grading and applying stay separate stages, like
  dedup's decide/apply split.

## 2026-07-12 — `srt geotag --relock` + `immy tags sync`

### Found

- **Drone/video clips silently missing location and gear tags in Immich.**
  Root-caused live on n5: `asset_exif.latitude/longitude` was populated for
  most DJI clips (map pin renders correctly) but `lockedProperties` was
  empty and `country`/`state`/`city` were NULL — an unlocked, ungeocoded
  state that falls through both existing safety nets (`srt geotag` skips
  any row with *a* coord already; `srt geocode` requires the lock token as
  proof-of-ownership before touching a row). Same video/XMP blind spot
  (`docs/TELEMETRY.md`) also silently drops notes-derived tags
  (`Gear/Camera/DJI FC8282`, trip/event tags) for every video asset — XMP
  sidecars work for photos only, and `promote --tag` only pushes whatever
  flat list was passed on that one invocation, not the trip's full notes
  `tags:`.

### Added

- **`immy srt geotag --relock`** (`srtgeo.py`, `cli.py`): repairs clips that
  already carry a DB coord but were never locked — locks + reverse-geocodes
  them, but only when the existing DB coord is within 2 km of the
  independently-computed `.SRT` fix (`_RELOCK_TOLERANCE_M`), so a location
  pinned by hand in the app is left alone (`skip-mismatch`).
- **`immy tags sync <trip> [--write]`** (new `tagsync.py`): pushes a trip's
  full notes `tags:` to every one of its assets via Immich's native Tag API
  — the durable, video-safe channel `trip-tags-from-notes`'s XMP write can't
  reach. Per-file gear-tag matching (`rules/trip_tags.tags_for_file`, `_propose`) is
  shared with the XMP rule so the two channels can't disagree.
- **Library-wide backfill run**: **865 GPS relocks + 25 fresh geotags** across
  the 30 trips with drone footage; **7,702 tag placements** across all 65
  notes-bearing trips (distinct tagged assets in `tag_asset` went 5,332 → 7,489
  — spot-checked via direct Postgres query, not just the CLI's own summary).

### Fixed

- **`ImmichClient.upsert_tags` silently dropped every hierarchical tag
  attachment.** The first `tags sync --write` backfill reported "tagged 7,702
  asset(s)" with exit 0 and zero errors — but `tag_asset` row counts hadn't
  moved. Live `GET /api/tags` showed why: for a hierarchical tag (`Gear/
  Camera/DJI FC8282`), Immich's response `name` field is just the leaf
  segment (`"DJI FC8282"`); the full path lives in `value`. `upsert_tags`
  keyed its `{name: id}` return by `name`, so every caller's lookup by the
  full path missed, and the follow-up `tag_assets` call for that tag never
  fired — while `upsert_tags` itself legitimately succeeded (the tag rows
  really were created), so nothing surfaced as an error anywhere. Fixed to
  key by `value` (falls back to `name` if absent). `tag_sync_folder` also no
  longer marks a row `"tagged"` until *after* confirming the API actually
  returned an id for every tag it needed (`"tag-failed"` otherwise) — this
  exact gap (optimistic status before the write) was independently flagged
  by a Codex pre-commit review before the root cause was even isolated. A
  follow-up Codex pass on the fix itself then caught a second, narrower gap
  in the same spirit: `tag_assets()`'s own per-asset response was still
  unchecked, so a genuine attach failure (as opposed to `error="duplicate"`,
  which is expected/idempotent) would've still read as `"tagged"`. Closed by
  inspecting `tag_assets()`'s result too. Re-ran the full backfill after each
  fix; verified via Postgres, not just the CLI's exit code — final state:
  7,489 distinct assets carrying a native tag, `tag-failed=0` across all 65
  trips.

### Also

- **`immy tags camera <trip> [--write]`**: backfills the Details panel's
  blank "Camera" row for DJI clips (`asset_exif.make`/`model`, empty because
  the MP4 container carries neither Make/Model nor an Encoder atom —
  confirmed empty live, not assumed). Verified live with a
  `srt verify-channel`-style probe first: unlike GPS, Immich's metadata
  refresh doesn't clobber an *unlocked* make/model write either — locked
  anyway as a safety net.

  First version derived (make, model) by splitting the trip notes'
  `Gear/Camera/<code>` tag on the first space — shipped, backfilled 1,813
  assets, then the user immediately caught it: raw codes like `FC8282`
  aren't human-readable, and this repo already has an owner-confirmed
  friendly-name table (`devices.py`, from a June 23 commit — bypassed
  entirely because this new code never looked for it). Rebuilt on
  `devices.resolve()` instead: primary signal is the file's own raw EXIF/
  Encoder; falls back to the notes gear tag (itself resolved through the
  same table, not used raw) only when the file has no signal at all —
  which is genuinely the common case for DJI video.

  That surfaced a second, independent bug: `devices.py`'s friendly names
  already included "DJI" (`("DJI", "DJI Air 3")`), and make is *also*
  `"DJI"` — Immich concatenates make+model for display, so this rendered
  as "DJI DJI Air 3". Pre-existing (9 assets already had it via the normal
  `immy process` ingest path, dating to June); fixed at the root by
  stripping the redundant prefix from every table entry.

  Backfill made self-correcting in two directions once both bugs were
  fixed: an asset already locked by a previous run of this command gets
  silently re-corrected if the resolved value changed; an asset with an
  *unlocked* existing value is left alone unless that value is itself a
  known-raw code the table maps to something different (confident lookup,
  never a guess). That upgrade path caught 632 more pre-existing assets
  across the library carrying a raw code from before `devices.py` existed,
  plus a previously-unmapped DJI video Encoder string (`"DJI Mini5Pro"`,
  no space — 518 assets). ~130 assets remain stuck on a raw code behind a
  pre-existing, unrelated data-integrity issue found along the way: 624
  duplicate `asset` rows sharing an `originalPath` across the library make
  asset resolution nondeterministic for those files — out of scope here,
  belongs to the separate `immy dedup` pipeline.

  Two independent AI reviews (Codex, Grok) on the *first* version of this
  feature weren't run before the user caught the raw-code issue — a gap in
  process, not tooling: reviews happened on the GPS/tags fix, this feature
  shipped afterward without a fresh pass.
- **Legacy tag cleanup**: found 78 malformed flat tags across the library
  with a literal `|` in their name (e.g. `Gear|Camera|DJI FC8282`) predating
  this session — a past bug (unrelated to the one above) that passed
  hierarchical-looking names through the wrong separator. Deleted the 59
  that were fully redundant with the correct `/`-hierarchical tag (verified
  every attached asset already carried the correct equivalent first); left
  19 alone where the pipe tag's assets weren't fully covered (renamed trips
  whose old event name has no current equivalent, or gear not reflected in
  current notes) rather than guess.

Reviewed twice more end-to-end (Codex resumed thread + a fresh Grok pass)
against the merged commit — both independently confirmed no further
correctness issues; findings were operational hardening (non-zero exit on
partial failure, per-trip error isolation for multi-trip runs) and applied.

## 2026-06-23 — `immy match` + snapshot v2

### Added

- **`immy match <inbound>`** (`match.py`, `cli.py`): read-only, fully offline
  triage of a folder about to be imported. Reports per top-level **subfolder**
  and per self-clustered **event**: which files are already in Immich (dedup
  via the `find-duplicates` classifier), which belong to an existing trip
  (`matched`/`extends`), and which are `new`. `⚠ spans multiple trips` flags a
  folder that straddles trips. `--thorough` hashes every file (catches
  renames); `--max-km`/`--max-gap-hours` tune placement. `--fast`/`--no-verify`
  (`HashMode.FAST`) trusts a name+size hit and skips SHA1 — turns a ~2 TB
  already-promoted tree from ~50 min (SHA1-bound) into ~2 min (exiftool-bound).
- **Existing trips reconstructed offline** (`build_existing_trips`): every
  snapshot album becomes a trip keyed by name, with **IQR-fenced date bounds**
  + **median centroid / 90th-pct radius** so one misdated/mislocated asset
  can't blow a trip's window to 9 years and date-match everything; un-albumed
  assets are re-clustered with the `immy cluster` sweep.
- **GPS-less fallback**: drone/video clips with no EXIF GPS are placed
  **date-only** (labelled lower-confidence); tally points at `immy srt geotag`.
- **Snapshot schema v2** (`snapshot.py`): `assets` gains `lat/lon/city/country`;
  new `albums` + `album_assets` tables. `fetch_albums` keeps **every** album
  (real albums are trip-named but carry no `immy-cluster` marker — marked-only
  would drop them all). `require_schema` rejects v1; `find-duplicates` stays
  v1-compatible. Docs: [docs/MATCH.md](docs/MATCH.md). Reviewed by Codex + Grok.

## 2026-06-19 — SRT telemetry pipeline (`immy srt`)

### Added

- **Full DJI `.SRT` track parser** (`srt.py`): `parse_track()` →
  per-frame `SrtFrame` (t, lat/lon, `rel_alt`/`abs_alt`, iso/shutter/fnum/
  ev/focal_len). Handles the combined `[rel_alt: .. abs_alt: ..]` bracket,
  legacy `[altitude:]`, and `GPS(...)`. `first_valid_fix()` skips `(0,0)`
  pre-lock frames (the takeoff point). `parse()` keeps its first-fix API
  (streams cues, early-stops) so `dates`/`backfill_dates`/`rules.dji_srt`
  are unchanged — and now gain the 0,0-skip for free.
- **Track sidecars** (`track.py`): `<stem>.gpx` (GPX 1.1, round-trips
  through `rules.geotag_from_gpx`) + `<stem>.track.json` (per-frame
  telemetry + summary). Placed via new `WritablePaths.gpx_path` /
  `track_json_path` — on the NAS they mirror under `sidecars_root`, never
  beside the `:ro` originals.
- **`immy srt` CLI group**: `track` (emit sidecars), `geotag` (durable DB
  GPS from takeoff fix, dry-run by default, `--write` to apply), and
  `verify-channel` (the empirical probe below).
- **Durable video geotag** (`srtgeo.py`): `UPDATE asset_exif` lat/lon +
  append `latitude`/`longitude` to `lockedProperties` — mirrors how
  descriptions are made refresh-proof. Idempotent (skips assets that
  already carry DB coords); triggers Immich's reverse-geocode.
- **Caption context** (`captions.caption(context=…)`): drone clips now feed
  `~{rel_alt} m above ground` + place (notes `location.name`, else cached
  reverse-geocode) into the VLM prompt. Threaded through both caption paths
  in `process.py`; non-drone media stays byte-identical.
- **Reverse-geocode** (`geocode.py` + `immy srt geocode`): replicates Immich
  v2.7.5 `MapRepository.reverseGeocode` against the *same* Postgres —
  `geodata_places` nearest within 25 km (`earthdistance`), `naturalearth_countries`
  polygon fallback — and maps `countryCode`→name via the vendored
  i18n-iso-countries 7.6.0 'en' dataset. So drone clips get country/state/city
  **identical** to the rest of the library, fully offline. `srt geotag` writes
  place inline; `srt geocode [--prefix]` backfills from DB coords (no files).
- 27 new tests (`test_srt`, `test_track`, `test_srtgeo`, geocode + caption-context);
  multi-frame DJI fixture. 471 pass.

### Findings

- **verify-channel result (run live on n5, DJI_0073.MP4)**: for VIDEO
  assets a metadata refresh **clobbers an unlocked `asset_exif` GPS to
  NULL** (Immich re-reads container tags, finds none — XMP sidecars are
  images-only). An `UPDATE` **+ `lockedProperties` lock with tokens
  `latitude`/`longitude` survives**. So the XMP-sidecar geotag from the old
  `dji-gps-from-srt` audit rule never reaches the DB for drone videos —
  `srt geotag`'s lock is the only durable channel. Probe is non-destructive
  (restores the asset).
- **First live run (2024-02-peru-bolivia)**: 230 NULL-GPS drone clips tagged
  from their SRT takeoff fix; GPS landed + locked, **survives refresh
  (gps_lost=0)** → map pins now work.
- **Locked coords are never auto reverse-geocoded — confirmed in source.**
  Immich v2.7.5 `metadata.service.ts` only geocodes coords read *fresh from
  the file* (`if (hasGeo(fileExif))`), never the DB value; our drone videos
  have no file GPS and read-only originals, so no Immich path (refresh *or*
  the asset-update API) will ever geocode them. The `PUT /api/assets/{id}`
  route is also destructive on `:ro` originals — it queues a `SidecarWrite`
  that can't land and the live test *wiped* a good geotag (restored).
- **Resolved by self-geocode from Immich's own geodata.** Cross-checked the
  port against 1,500 already-geocoded assets: **country/state/city 100 % match**.
  Backfilled the 230 peru-bolivia clips: country 100 % (Bolivia 163 / Peru 67),
  city 97 (rest are >25 km from any geodata place → country-only, same cutoff
  as Immich). GPS stays locked + intact.

## 2026-06-19 — backup automation: nightly n5→vv mirror (Phase 3)

### Added

- **`immy/deploy/n5/backup/`** — `nightly-mirror.sh` + `mirror.env.example` +
  `README.md`. Self-contained nightly job implementing Phase 3 of the primary
  swap: perm self-heal (originals → `faeton:faeton 755/644`) → `pg_dumpall`
  (verified, with a `RESTORE-RECIPE.txt`) → atomic ZFS-snapshot rsync of
  `originals/` + `media/{library,profile,upload}/` to vv → push dump to `vv:db/`.
  `flock` (no overlap), Healthchecks dead-man's-switch (`/start` + success +
  `/fail` with log tail), `--max-delete=200` guard, `DRY_RUN` toggle.

### Findings

- **Native TrueNAS tools can't own this**: ZFS Replication needs a ZFS receiver
  and vv is a Synology (btrfs); SCALE Rsync Tasks have no pre/post hooks (can't
  sequence dump/perm/ping around the copy); `pg_dump` has no native task. So the
  orchestration is a script, run by a **native Cron Job** (id 3, `0 5 * * *`,
  user `faeton`); existing Periodic Snapshot Tasks stay independent.
- **Run as faeton + `sudo`, not root**: faeton's vv ssh key already works, so we
  avoid setting up root→vv auth; `sudo` covers zfs/docker/chown. Pushing as
  faeton@vv (no `--numeric-ids`) lands files owned by vv's own faeton — the perm
  story vv wants, for free.
- **`media/library` is `0777`** (Immich chmods everything world-readable), so a
  faeton-run mirror reads it fine — the perm self-heal only needs `originals/`.
- **Config-file vs env footgun**: sourcing `mirror.env` (which carries
  `DRY_RUN=0`) clobbered a `DRY_RUN=1` passed on the command line, so the first
  "dry-run" did a full live mirror. Fixed: the CLI/env `DRY_RUN` is captured
  before sourcing and wins. (Pipeline thereby validated live end-to-end.)

### Deployed

- Installed to `n5:/mnt/tank/scripts/immich-mirror/` (faeton-owned; lock + logs
  live beside the script). Cron job id 3 materialized in `/etc/cron.d/middlewared`.
  **TODO (user):** paste the Healthchecks check URL into `mirror.env` (`HC_URL=`)
  — until then a *missed* run won't alert.

## 2026-06-19 — immy runs on the N5; read-only-originals refactor

### Findings

- **Immich's ML container has no published port** (`immich_machine_learning:3003`,
  internal-only on `ix-immich_default`) — so to use Immich's own CLIP, immy must
  run *inside* that docker network, not over the tailnet. That's the whole reason
  for packaging immy as a container on the NAS.
- **`host.docker.internal` (host-gateway) is unreliable here**: reaching a
  published port from a container on `ix-immich_default` hits a hairpin-NAT bug —
  TCP connects but HTTP responses get reset. Fix: address every backend by
  container name on the shared network (attach Ollama + the qwen-asr shim to it).
- **`process` wrote all state under the trip folder** (`.audit/` journal, marker,
  heartbeat, staged derivatives; `.srt`/`.xmp` next to media) — fatal on the NAS
  where originals are a read-only mount of the live external library. Even a
  captions-only run failed, because the caption `.xmp` mirror wrote beside originals.
- **Scope decision** (codex+grok review): on the NAS immy does **captions +
  transcripts only**; Immich keeps doing its own CLIP/faces/thumbnails (ML on by
  default). Cross-machine dedup (Mac ⇄ NAS) already works via the DB AI-prefix
  check and existing-`.srt` detection — no new queue needed yet.

### Changed

- **Phase 6 — packaging** (`immy/Dockerfile.immy`, `immy/deploy/n5/`): standalone
  compose that joins `ix-immich_default`, lean image (`--no-deps`, no onnx/mlx),
  one-shot `docker compose run --rm`. Not folded into the TrueNAS-managed stack.
- **Phase 6.1 — writable-state refactor**: new `state_root` / `sidecars_root`
  config (env `IMMY_STATE_ROOT` / `IMMY_SIDECARS_ROOT`) + `immy/paths.py`
  resolver threaded through every write site. Unset = Mac path **byte-identical**;
  set = state → `state_root`, sidecars → `sidecars_root`, originals stay read-only.
- `run-batch.sh` defaults to `--with-captions --with-transcripts --no-clip --no-faces`.
- 444 tests (+9: defaults-match invariant, NAS-mode redirect, a `chmod 0555`
  read-only-trip proof). Verified on the N5: build, four-backend reachability,
  dry-run with `originalPath` anchored correctly.
- The heavy opportunistic-worker queue (claim ledger, `immy worker pull`, GPU
  scheduler) is **deferred** until the consolidation import waves need it.

## 2026-06-11 — mass "Error loading image": paused thumbnail queue

### Findings

- **4,961 assets across 16 trips had no thumbnail/preview `asset_file`
  rows** (Svalbard 2,393; all five pacific trips; les-arcs; scotland;
  both norways; several 2024 trips) — grid tiles and the full-screen
  view both showed "Error loading image".
- Root cause was a chain: these assets were registered while their
  originals were still offline, so promote fell back to queueing
  `regenerate-thumbnail` jobs for Immich to run server-side — but the
  server's `thumbnailGeneration` queue was **paused**, with 8,821 jobs
  silently accumulating. The fallback never executed.
- The paused queue also broke **phone-app backups** (internal-storage
  uploads, `libraryId IS NULL`): Immich is the only thumbnail generator
  for those, so 9 iPhone HEICs sat with no derivatives at all.
- ~640 server assets (DJI LRF/LRV proxies, HYPERLAPSE stills) are
  offline+trashed — expected: their local sources were deliberately
  deleted; the library scan retired them server-side.

### Changed

- `immy repair-thumbs` across all trips: 4,920 thumbnails+previews
  regenerated locally and upserted (9,840 rows), 41 Svalbard assets had
  no local source. Full-library probe after: 10 broken of 7,462.
- Emptied the stale 8,821-job queue, queued regeneration for the 10
  remaining (9 phone HEICs + 1), **resumed `thumbnailGeneration`** —
  all verified loading. The queue must stay unpaused: promote's
  offline-asset fallback and every future phone backup depend on it.

## 2026-06-11 — Immich metadata refresh destroys descriptions

### Findings

- **Immich v2 rebuilds `asset_exif` from file tags on metadata
  extraction and overwrites every field not in `lockedProperties`.**
  Descriptions written via direct SQL carry no lock and no sidecar →
  a library scan after the 9-trip upload wiped 338 synced descriptions,
  replacing them with camera-embedded junk: '' (videos), 'default'
  (DJI), 'DCIM\…' paths or the file's own name (Insta360).
- **Video descriptions ignore XMP sidecars entirely** (v2.7.5 source:
  video path reads only `videoTags.Description || Comment` from the
  container). `PUT /api/assets` is self-defeating for videos —
  SidecarWrite unconditionally unlocks the field, then queues
  re-extraction: 197 API-pushed video descriptions were wiped again
  within minutes. Images survive (photo path prefers sidecar tags).
- The only durable, non-file-mutating mechanism for video descriptions
  is appending `'description'` to `asset_exif."lockedProperties"` via
  SQL in the same statement as the write — no job cycle unlocks it.
- Confirmed with Codex (immich source review) + Grok consults.

### Changed

- All immy description writes now also lock the field; camera
  boilerplate is treated as overwritable by every guard
  (`captions.is_camera_boilerplate`).
- Captions/transcript excerpts are mirrored into the local
  `basename.xmp` (`_mirror_description_to_xmp`) — image-path protection
  that travels with promote's rsync; skipped when the DB guard refused
  the write.
- New `tools/reconcile-descriptions.py` — server-vs-sink description
  diff; pushes diverged values (images via API, videos via SQL+lock);
  dry-run by default, ambiguous cases never touched.
- Offline-sink drain of the full library: 2 120 entries replayed
  (argentina's 8 missing CLIP `.npy` regenerated from staged previews,
  1 stale faces ref dropped); descriptions reconciled server-side.

## 2026-06-10 — full-library transcript run (overnight batch)

### Findings

- **Full sweep complete**: all 61 trips through `immy process --offline
  --with-transcripts --with-captions --captions-fill-missing`. 676 new
  sidecars (350 en / 300 ru / 26 uk), 3 122 gated skips (DJI denylist,
  Tesla dashcam no-audio, silent clips), zero errors. Library now at
  100 % coverage on every phase including captions; `immy bloat` scan
  found zero transcode candidates.
- **"Wood Wood"** is a new Whisper noise hallucination on water/splash
  audio (la-manga, blue-lagoon, peru) — emitted as runs of identical
  sub-second cues.
- **Blank segments defeated the loop collapse**: `format_srt` computed
  decode-loop runs over the raw segment list, where Whisper's interleaved
  blank segments break a run of identical cues — "Wood Wood" ×7 survived
  write-time scrub. 792 such cues landed across 67 fresh sidecars before
  the fix.
- **Stale transcript journal entries hide real gaps**: 88 entries pointed
  at sidecars deleted long ago. 57 were intentional (Insta360 twin dedup
  keeps only the `_00_` master), 5 belong to arbiter-dropped groups
  (stay dormant by design), 26 were genuine orphans — among them the 18
  antarctica clips journaled as Faroese/Nynorsk by a pre-constrained-
  detect run in April.

### Changed

- `fix(transcripts)`: loop detection now runs on the non-empty cue
  stream, matching the written SRT's view.
- Post-run scrub applied: 792 loop cues removed from 67 new sidecars.
- 26 orphaned journal entries cleared and re-transcribed under the
  constrained ru/en/uk language detect.

## 2026-06-10 — library-wide verification sweep, in-cue word loops

### Findings

- **Library sweep** (`tools/verify-transcripts.py`, 165 sidecars / 98
  unique audio tracks): 24 low-agreement files judged, 6 drops suggested,
  4 of them over-drops on human review (the judge's known failure mode —
  real conversation with one garbled line). 2 genuine silence
  hallucinations dropped («До встречи!» ×4 over 2 min; "Thank you." ×5 on
  30 s-aligned cues).
- **In-cue word loops**: a decoder loop packed into a *single* cue
  («селфи» ×55, «девочкой» ×54 inside one segment) is invisible to the
  cue-level collapse, which needs ≥ 6 identical consecutive cues. Found in
  3 of the 4 over-dropped files.
- **Twin sidecars from different vintages diverge**: one Peru clip's
  judged sidecar was truncated at 1:56 while its LRV twin held the full
  14-minute conversation — the verifier's "A is a subset of B" reason was
  literally correct. Twin groups deserve a consistency pass when judged.

### Changed

- `feat(hallucinations)`: `collapse_word_runs()` — runs of ≥ 5 identical
  words (case-/punctuation-insensitive) within a cue collapse to the
  first occurrence, at `format_srt` write time and in
  `tools/scrub-srt-hallucinations.py` for existing sidecars.
- 8 sidecars hand-cleaned across 4 twin groups (la-manga, NZ, Peru ×2):
  in-cue loops truncated, the truncated Peru twin replaced with its full
  LRV transcript, garbage-only cues removed.

## 2026-06-10 — ASR engine bench, worst-80 redo, dual-engine verification

### Findings

- **Engine bench** (28 files / 3.1 h mixed ru+en travel audio; RTFx = audio
  seconds per inference second, model load excluded):
  - *Qwen3-ASR-1.7B* (mlx-qwen3-asr, GPU): RTFx 11.6. Quality winner — zero
    boilerplate, near-zero loops, only challenger to hear «Привет, бандит!»,
    and the only engine that preserves each language in mixed ru/en scenes.
    Flaws: occasionally flips a Russian phrase to English; hallucinated
    Dutch once on a very noisy clip.
  - *Whisper large-v3* (pipeline default, GPU): RTFx 16.9. Even with the
    new anti-loop decode flags it still *generates* «DimaTorzok» /
    «Продолжение следует» boilerplate on 14 of 28 files (write-time scrub
    catches it) and silently translates Russian speech inside en-detected
    files.
  - *GigaAM-v3* (`v3_e2e_rnnt`): RTFx **39 on pure CPU**, cleanest Russian
    of all, zero hallucinations — but unusable English. Ideal second
    opinion, not a sole engine. Hard input limit of exactly 25.0 s
    (400 000 samples); needs the GitHub install (PyPI 0.1.0 lacks v3) and
    Python ≤ 3.12 (onnxruntime pin).
  - *Canary-1B-v2* (NeMo): eliminated — RTFx 6.2 CPU-only on Mac (MPS
    rejects float64) and catastrophic repetition loops («Наконец» ×97) on
    exactly the files under treatment.
- **Insta360 twins**: dual-lens (`_00_`/`_10_`) + LRV proxy files of one
  clip carry identical audio. 54 of the 80 worst files were twins — 6.7 of
  15.1 audio-hours would have been transcribed in duplicate. Transcribe
  once per group, fan out.
- **LLM-judge non-determinism**: the LM Studio arbiter (gemma-4-31b-it)
  gives different verdicts across runs even at temperature 0, and
  over-drops files that contain one garbled line amid real conversation.
  Apply steps must execute saved, human-reviewed verdicts — never re-judge.
- **Whisper-vs-Qwen verification verdict**: across the 59 unique worst-80
  audio tracks, median word-level agreement between Qwen and an independent
  engine was 0.56; after judging + human review only 2 of 80 files were
  hallucination-only. Qwen held up on the hardest corpus, but the pipeline
  default stays Whisper until a broader sample is verified.

### Changed

- The 80 worst hallucinated transcripts (8 trips, 2023-11 → 2025-11)
  re-transcribed with Qwen3-ASR-1.7B: sentence-level cues with word-aligned
  timing, scrubbed via the shared hallucination filter, descriptions
  backfilled (empty-guard), journal entries record
  `engine: mlx-qwen3-asr/Qwen3-ASR-1.7B`. Two clips dropped as
  hallucination-only after dual-engine review and journaled as
  `arbiter-hallucination` skips.
- New `tools/verify-transcripts.py` — dual-engine transcript verification
  with twin-group dedup, agreement scoring, LM Studio arbiter (dry-run by
  default, `--apply` executes reviewed verdicts).
- New `docs/TRANSCRIPTS.md` — transcript pipeline, engine bench, twin
  dedup, verification design and its operational gotchas.

## 2026-06-09 / -10 — transcript hallucination root causes

- `fix(transcripts)`: two silent-kill API drifts in mlx-whisper 0.4.3 —
  `detect_language` returning a bare dict (KeyError killed every transcript
  via `on_transcript_error="skip"`) and `VideoInfo.duration_s` rename.
  Adopted `word_timestamps` + `hallucination_silence_threshold=2.0`
  (validated by A/B on the worst loopers).
- `feat(hallucinations)`: boilerplate matching is now substring- and
  case-insensitive («продолжение следует», DimaTorzok in any form);
  decode-loop collapse (same cue ≥ 6× consecutively) at SRT write time.
- `fix(journal)`: caption skip paths (same-model sink, DB `AI: ` prefix,
  kept-prior) now converge the journal, so resumed runs stop re-walking
  finished work. New `tools/audit-journal.py` reports true per-trip
  coverage from disk files (journal keys are path-hashes — renames orphan
  them) with guarded stale-key pruning.
- Verified full-library state: 6 997 live assets at 100 % derivatives /
  CLIP / faces / captions; 1 115 stale journal keys pruned; 23 LRF ghost
  sink files quarantined (none existed server-side).

## 2026-06-08 — caption pipeline robustness

- `feat(captions)`: parallel `--caption-workers` pool; all DJI `.LRF`
  proxies dropped at ingest.
- `fix(captions)`: re-encode retry on LM Studio invalid-image 400 (corrupt
  staged JPEG — validate with djpeg, PIL is too lenient); per-trip
  heartbeat during the parallel pool; stale heartbeats ignored.
- Captioner pinned to gemma-4-31b-it via LM Studio (7-captioner bench:
  ~3 s warm, quality on par with cloud CLIs; qwopus ~14× slower, reserve
  for OCR-heavy trips).
