# Build plan

Phased so each phase is independently useful. Stock Immich first, sidecar
bolted on in layers. Each phase has a "done when" so we don't drift.

Rough effort estimates are evening-hours for a single person.

Current not-yet-shipped backlog is summarized in [TODO.md](TODO.md).
This file keeps the phase-by-phase rationale and acceptance criteria.

## Phase 0 — Base stack ✅ done

**Stock Immich on the Syno, no custom code.** Running under Container Manager,
docker project `${COMPOSE_PROJECT}`, all state under `${DEPLOY_ROOT}`. Full as-built
notes in [DEPLOY.md](DEPLOY.md); acceptance checks in [TESTING.md](TESTING.md).

Deviations from the original plan:
- NVMe on this DS923+ is deployed as **SSD read/write cache** (md3 RAID1) for
  `/volume1`, not a separate storage pool. Postgres lives on `/volume1`
  (btrfs + `chattr +C` on the PG data dir) and benefits from the cache.
- Access is **Tailscale-first** (99 % of traffic). No LAN firewall rule for
  2283 needed; DSM reverse-proxy + Let's Encrypt deferred until we actually
  want a public hostname.
- Storage template enabled: `{{y}}/{{y}}-{{MM}}-{{dd}}/{{HH}}{{mm}}{{ss}}-{{filename}}`.
- Originals share mounted into the server container at `/mnt/external/originals`
  (read-only), ready for the Phase 1b mount adapters and Phase 2 ingest funnel.

**Closed 2026-04-19**:
- ✅ External Library `<external-library-name>` (`<external-library-uuid>`)
  created in Admin → Libraries with import path `${EXTERNAL_ORIGINALS_MOUNT}`;
  scan runs clean against the empty tree (`assetCount: 0`).
- ✅ iOS Immich app logs in over `${IMMICH_URL}`
  and one end-to-end upload hit the Immich timeline.
- ✅ First `pg_dumpall` (16 MB gzipped) + `library/` tarball (92 MB) landed in
  `${BACKUP_ROOT}`; `gunzip -t` passes. Drill documented in
  [DEPLOY.md](DEPLOY.md#backup) (note: docker compose on DSM needs `sudo`).
  Off-NAS copy still manual until Hyper Backup is wired up.

## Phase 1 — Mac as burst ML node — **abandoned**

Original scope: Mac exposes `MACHINE_LEARNING_URL` endpoint, Immich on
NAS fails over to NAS ML when Mac unreachable. Explored 2026-04-19 and
dropped because:

1. The ML endpoint only speeds up the small part of Immich's pipeline
   (CLIP/face), not the expensive part (thumbnail + proxy generation
   on originals). For drone video — our biggest pain — it doesn't help.
2. `immich-ml-metal` upstream is AI-authored alpha. The actively-
   maintained `epheterson/immich-apple-silicon` wraps it but is a full
   microservices replacement (needs PG + Redis exposure + SMB mount),
   which breaks the "Mac offline = fine" contract from the original
   Phase 1 scope.
3. The real Mac-side wins we care about (pre-transcode big videos,
   pre-compute ML for Mac-originated content, keep NAS-originated
   iPhone uploads on NAS) push toward a different architecture — see
   Phase Y below.

`immy bloat transcode --apply` (Phase 2c, shipped) already solves the
big-video-on-NAS problem at the pre-ingest layer, which was the main
motivation for Phase 1. Phase Y extends the same pattern to ML.

## Phase Y — direct-to-Immich-DB pre-processing (Mac-native) — in design

**Compute everything on the Mac, write directly to Immich's Postgres,
skip Immich's own processing queue for Mac-originated content.**

Model:

```
immy audit   — metadata fixes (XMP sidecars, shipped)
immy bloat   — pre-ingest HEVC transcode (shipped)
immy process — NEW: checksum + thumbnail + preview + CLIP + faces, all on Mac
immy promote — rsync originals + derivatives to NAS + direct PG INSERTs,
               no library scan triggered for Mac-handled trips
```

iPhone uploads (direct to NAS) keep running through Immich's own
microservices on NAS — we don't touch them. No queue race, no coexistence
problems, no PG/Redis port exposure.

**Research input**: `epheterson/immich-apple-silicon` (brew-installed as
`immich-accelerator`) packages everything we need — the extracted
Immich server at `/opt/homebrew/Cellar/immich-accelerator/1.4.8/libexec/`
gives us exact schema, path conventions, and reference implementations
for every processor. We mine that code for knowledge, write our own
Python equivalents integrated into `immy`, then uninstall the brew
package once we're standing on our own.

**Breakage contract**: when a Y workflow stops working, that's our
signal Immich upgraded its schema / path conventions / ML model. We
bump the pin, update the affected slice, re-test. Version-bump is the
only supported maintenance path — no "gracefully handle unknown schema".

### Iteration ladder

| # | Scope | Done when | Status |
|---|---|---|---|
| **Y.0** | **Research** — read accelerator's bundled server code, document: exact SQL for new asset ingest, derivative path conventions, ML model invocation surface, checksum algo, scan-vs-direct-insert behaviour, Immich version pinned. | Single internal doc under `docs/` maps every call Immich makes on ingest to the exact table rows + file paths it produces. | ✅ 2026-04-19 → [IMMICH-INGEST.md](IMMICH-INGEST.md) (1037 lines, cited) |
| **Y.1** | `immy process <trip>` computes checksum + EXIF rows, writes asset+exif to PG. No derivatives yet. `promote` stops triggering scan for Y-processed trips. | One trip lands in Immich UI with metadata-only entries, no timeline thumb, no errors on NAS logs. | ✅ 2026-04-20 — `immy process` lands asset + asset_exif via `psycopg3`, checksum = `sha1("path:"+container_path)`, ON CONFLICT DO NOTHING on `(ownerId, libraryId, checksum)`. Drops `.audit/y_processed.yml`; promote skips the scan POST when present. Smoke-tested against DS923+ PG: row + exif + GPS land as expected. 23 new unit tests (119 total passing). |
| **Y.2** | Thumbnail + preview generation via vips (Sharp). Written to NAS `library/thumbs/` via rsync, paths recorded in PG. | Same trip now renders timeline thumbs. | ✅ 2026-04-20 — `immy process --with-derivatives` (default) stages 250px WebP + 1440px JPEG via `pyvips` under `.audit/derivatives/thumbs/<userId>/<xx>/<yy>/<id>_*`. Marker extended with per-asset derivative records. `immy promote` rsyncs staged tree into `media.host_root` and UPSERTs `asset_file` rows with `path = media.container_root + /thumbs/...`. Compute (Mac) and upload (NAS) split so bad uplinks can resume without re-encoding. 14 new unit tests (133 total passing). |
| **Y.3** | CLIP embedding via mlx-clip. `smart_search` row per asset. | Text search finds Y-processed assets. | ✅ 2026-04-20 (code) — `immy process --with-clip` (default on) lazy-loads `mlx-clip` (pinned to the accelerator's commit), embeds the staged preview, L2-normalizes, and `INSERT … ON CONFLICT ("assetId") DO UPDATE` into `smart_search`. Embedding passes as a pgvector text literal; dim verified against `format_type(smart_search.embedding)` up-front. Model defaults to `ViT-B-32__openai` (512-dim), configurable via `ml.clip_model`. Skipped for videos / already-present rows. 18 new unit tests (151 total passing). Hardware smoke-tested 2026-04-20 on DS923+ PG via `2026-04-bolivia-smoke` (3 assets: 2 images + 1 video; 6 faces on group photo, CLIP rows per image, video duration/dims via ffprobe). |
| **Y.4** | Faces via InsightFace + CoreML (detection + recognition). `asset_faces` + `faces` rows. | "People" panel shows Y-processed assets. | ✅ 2026-04-20 (code) — `immy process --with-faces` (default on) runs Apple Vision's `VNDetectFaceLandmarksRequest` on the staged preview (Neural Engine), then ArcFace (`buffalo_l`, 512-dim) via `insightface` + `onnxruntime` with `CoreMLExecutionProvider`. Per face, one `asset_face` row (`sourceType='machine-learning'`, full bbox + image dims) plus a `face_search` row with the L2-normalized embedding as pgvector text. Re-runs DELETE existing ML faces first for idempotency; user-tagged (`sourceType='exif'`) rows untouched. Falls back to bbox-crop alignment when Vision can't return 5-point landmarks. Lazy imports keep `import immy.faces` cheap. 9 new unit tests (174 total passing). Hardware smoke-tested 2026-04-20 on DS923+ PG via `2026-04-bolivia-smoke` (3 assets: 2 images + 1 video; 6 faces on group photo, CLIP rows per image, video duration/dims via ffprobe). |
| **Y.5** | Video transcode proxy (if needed beyond `immy bloat`): `encoded_video_path` + derivative. | Videos in Y-trips play smoothly in web UI. | ✅ 2026-04-20 (code) — `immy process --transcode` (default on) probes with ffprobe, extracts a poster frame (`min(duration/2, 5s)`), generates thumbnail + preview from the poster, and re-encodes non-web-safe sources to h264/aac/mp4 at ≤720p via ffmpeg (`libx264 -crf 23 -movflags +faststart`). `asset.duration` overwritten with the ffprobe value; encoded-video staged under `.audit/derivatives/encoded-video/<userId>/<xx>/<yy>/<id>.mp4` and rsynced by `immy promote` alongside thumbs. Rotation from `side_data_list[].rotation` or legacy `tags.rotate` applied before width/height. Hardware smoke-tested 2026-04-20 on DS923+ PG via `2026-04-bolivia-smoke` (3 assets: 2 images + 1 video; 6 faces on group photo, CLIP rows per image, video duration/dims via ffprobe). |
| **Y.6** | Uninstall `immich-accelerator`. Confirm all of Y.1-Y.5 still works from our own code. | `brew uninstall immich-accelerator` + re-process a test trip → full ingest works. | ✅ 2026-04-20 — `immich-accelerator uninstall` + `brew uninstall immich-accelerator` removed runtime, launchd config, data dir, and ~700 MB of formula + deps (onnxruntime, opencv, gcc, …). Fresh `2026-04-bolivia-smoke2` trip (2 images, 4.9 MB) processed + promoted with `immy` alone: 2 assets, 7 faces, 2 CLIP rows, 4 asset_file rows in DS923+ PG. `immy` pipeline is now the only ingestion path. |

**Done when** (whole phase): a Mac-audited trip runs `immy audit → bloat →
process → promote` and appears in Immich fully processed, without
Immich's NAS microservices having touched it. Acc-free, queue-free.

## Phase 1b — Mount adapter framework (1–2 days)

**Pluggable remote storage with health checks and offline handling.**

- `autofs` / `.automount` for SMB/NFS shares (including remote Macs).
- `rclone mount` target for a cloud source, VFS cache on tier-0.
- Wrapper that reports health to the sidecar so scans don't hang on dead mounts.
- Catalog-only mode toggle per source.

**Done when**: can unplug an external drive mid-scan without Immich wedging;
thumbs continue to render from tier-0.

## Phase 2a — `immy` metadata forensics (3–5 evenings)

**Pre-ingest audit + tagging CLI.** Every trip folder runs through `immy`
before it becomes part of the Immich-visible `originals/`. Rules fix metadata
at the file / XMP sidecar level, so corrections travel with the files and the
Immich DB is always a downstream projection of truth on disk.

User-facing shape: `immy audit <folder>` (interactive) and `immy promote <folder>`
(rsync + library-scan trigger). State persists in `.audit/state.yml` so answers
aren't re-asked. See `SIDECAR.md` for the rule YAML schema and state format.

**Iteration ladder** — each lands something usable:

| # | Scope | Done when | Status |
|---|---|---|---|
| **2a.0** | Skeleton + fixtures. Typer CLI stubs, pyexiftool wrapper, empty rules engine, `tests/fixtures/` with hand-crafted problem trees, pytest green on one trivial test. Per-file audit output. | `immy audit ./fixtures/dji-srt-pair` prints an EXIF table + per-file flags, exits 0. | ✅ shipped |
| **2a.1** | Four HIGH-confidence rules, XMP sidecar writer, `state.yml` + folder notes file persistence, JSONL audit log. Rules: (1) `dji-gps-from-srt`, (2) `dji-date-from-srt`, (3) `date-from-filename-vid-img` (VID/IMG/DJI/MVI/PXL), (4) `insta360-pair-by-ts-serial`. | Audit on real drone + 360 fixtures writes correct XMP sidecars; re-run is a no-op; folder notes file created with trip identity block. | ✅ shipped |
| **2a.1+** | Folder-notes-driven rules shipped early from 2a.3 + 2a.5 because trip-scoped metadata was the concrete blocker on real data: `trip-gps-anchor` (HIGH when `location.coords` in front-matter; interactive LOW prompt writes coords back to notes), `trip-tags-from-notes` (HIGH — `tags:` list → XMP `HierarchicalSubject` + `Subject`), `trip-timezone` (HIGH — `timezone:` IANA zone → tz-suffixed `XMP:DateTimeOriginal`). Per-field dedup (specific > general). Two-pass apply handles rule dependencies (e.g. `trip-timezone` needs a date written by `dji-date-from-srt` in the same run). Scaffold notes auto-populate `location:`, `timezone:`, and suggested `tags:` on first audit. | `immy audit --write` on `Mau-Lions-1` (66 Nikon Z50_2 frames, no in-camera GPS) writes XMPs with Casela coords, `Indian/Mauritius`-suffixed dates, and `Events/Gear/Source` tags. Re-audit: 0 pending. | ✅ shipped |
| **2a.2** | Date-authority resolver (`exif > companion > filename > mtime`) + folder-median clock-drift rule (MEDIUM tier, flags files >24 h from median with source + delta in the reason line) + MEDIUM-confidence y/n prompter + `--yes-medium` flag. Per-tier dedup so a MEDIUM finding survives even when a HIGH rule also claims the same XMP field (user accepts MEDIUM only after HIGH has converged). `--yes-high` deferred — HIGH today is unconditionally applied under `--write`; the flag only becomes meaningful once watcher mode (2a.6) introduces HIGH prompts. Cross-camera variant (`clock-drift-by-camera`) ships alongside: when ≥2 camera groups each have ≥3 samples, picks the GPS-richest group as reference and proposes a per-camera delta; findings share a `group` key so the prompter asks once per camera, not per file. Folder-median rule defers when multi-camera. | `clock-drift-simple` fixture (3 coherent + 1 four-day outlier) surfaces one MEDIUM prompt with human-readable reason. `--yes-medium` → exiftool writes XMP:DateTimeOriginal = folder median; re-audit clean. Interactive y/n/skip honoured. Two-camera folder with a 3h offset surfaces one batch prompt; accept writes delta to every off-group file. | ✅ shipped |
| **2a.3** | MEDIUM auto-propose of missing "obvious" tags for folders where the notes `tags:` list was hand-edited. Rule = `tag-suggest-missing`: compares existing tags to what the scaffold *would* produce from current EXIF/filenames, proposes any tag whose category (everything before the last `/`) is entirely absent from the user's list. Opt-out via `tag_suggestions: off` front-matter. Introduces a new `write_notes` action: accepted patches merge into notes front-matter and cascade to XMP via `trip-tags-from-notes` on the next apply pass. Immich-round-trip assertion of the full hierarchy lands with 2a.4 (`promote`). | `tag-suggest-missing/` fixture (Nikon JPGs + TRIP.md with only `Events/CustomEventLabel`) surfaces one MEDIUM prompt. `--yes-medium` merges `Gear/Camera/...` + `Source/DSC` into notes, cascade re-fires `trip-tags-from-notes`, XMP sidecar carries the full set. Re-audit clean. | ✅ shipped |
| **2a.4** | `immy promote` (aliases `push`, `pub`): rsync trip folder to `originals_root`, trigger Immich `POST /api/libraries/:id/scan`, then `POST /api/stacks` per `.insv` ↔ `.lrv` pair (`.lrv` primary). Config from `~/.immy/config.yml` (or `$IMMY_CONFIG` / `--config`). `.audit/` excluded from rsync. Guard rail: refuses with exit 1 if HIGH findings are pending (override via `--force`). `--dry-run` skips all writes and API calls. Immich section of config is optional — missing creds degrade to rsync-only. | `promote --dry-run` performs zero writes and zero API calls. `promote` on an audited trip rsyncs, calls scan once, and calls `/api/stacks` once per Insta360 pair with the `.lrv` asset ID as primary. Re-running is a no-op on disk; Immich gets re-notified (cheap). | ✅ shipped |
| **2a.5** | LOW/HIGH advisory rules: ✅ interactive `trip-timezone` prompt; ✅ `export-date-trap`; ✅ `trip-timezone-guess-gps`; ✅ `trip-timezone` respects per-file `EXIF:OffsetTimeOriginal`; ✅ `bloat-candidate`; ✅ `geotag-from-gpx` (HIGH — nearest-time-within-5-min match against any `.gpx` track in the folder); ✅ `makernote-present` (LOW note — flags vendor MakerNote blocks, emits `exiftool -MakerNotes=` suggestion, never modifies originals); ✅ `geocode-place` (HIGH `write_notes` — `location.name` → `location.coords` via Nominatim with 5 s timeout, cached at `~/.immy/places.yml`, silent on offline). | Two real trips each go through with <10 % LOW-confidence prompts. | ✅ shipped |
| **2a.6** | Watcher mode: `launchd` plist, debounced `watchdog` on `~/Documents/Incoming/`, non-interactive `--yes-high`. | Drop a folder in Incoming, walk away; return to either clean promotion or a `NEEDS_REVIEW` file listing open questions. | deferred — build when actually needed; today's backlog is big enough that manual `immy audit` / `promote` per trip is the faster path. |
| **2a.7** | Web routes for the LOW-confidence cases that don't fit a terminal: `/audit` on the sidecar web app, map picker, thumb grid. | Every rule class has either a terminal or web answer path. | pending |

**Done when** (whole phase): dropping `~/Documents/Incoming/<TripName>/` yields
a correctly-tagged `originals/<TripName>/` with EXIF + XMP sidecars that Immich
reads without further fixes, and no metadata decision lives only in the DB.

## Phase 2 — Ingest funnel (1–2 days)

**Inbox → originals pipeline.** Phase 2a's `immy` is the Mac-side entry point
for trip archives; this phase adds the passive/scheduled lanes for Apple Photos
and per-device SD-card drops.

- Inbox watcher (polling, since inotify over SMB is unreliable).
- `osxphotos export --update` scheduled on the Mac, writing to
  `/library/inbox/apple/<date>/` with full sidecars (people, keywords).
- `icloudpd` for iCloud-only items.
- Per-device rsync targets for cameras (one folder per camera).
- Normalisation step: exiftool header-only read, extract embedded JPEG
  preview, generate sidecar stub, move to `originals/`.

**Done when**: dropping files into inbox results in Immich showing them with
correct EXIF within a minute, without full-file reads.

## Phase 2b — Lazy preview extractor (2–3 days)

**The "never read the original" optimisation.**

- Embedded JPEG preview harvest from RAW (CR3/ARW/NEF/DNG) via exiftool.
- HEIC / iPhone thumbs.
- MP4 moov-atom + first-GOP poster.
- Insta360 `.lrv` pairing.
- DJI `.LRF` + `.SRT` parsing (telemetry → XMP sidecar).
- Fallback: proxy generation via ffmpeg streaming range reads.

**Done when**: ingest of a 20 GB ProRes clip completes in seconds (embedded
preview) and the full 1080p proxy generates asynchronously in the background.

## Phase 2c — Bloat detector + batch re-encode (CLI-first — shipped 2026-04-19)

**Find files that are uselessly huge, confirm in groups, transcode.**
CLI-first (no sidecar web app yet). Lives in `immy` as `immy bloat list` /
`immy bloat transcode`, reusing the Phase 2a detection logic from the
`bloat-candidate` rule. The group-confirm UI is a terminal prompt; the
web version lands later with the Phase 5 gap-fill UI.

Shipped surface:
- `immy bloat list <folder>` — scan, group by parent dir, show per-group
  and total "would save" figures.
- `immy bloat transcode <folder>` — per-group y/n, `ffmpeg -c:v
  hevc_videotoolbox -tag:v hvc1 -b:v <target>` to `<stem>.optimized.<ext>`,
  ffprobe verify (duration ±0.5s + stream count match).
- `--apply` (off by default) — atomic replace: original → `<name>.original`,
  optimized → source path, `.transcode.json` receipt with pre-sha256,
  pre/post size, codec family, and dimensions.
- `--dry-run`, `--yes` for non-interactive runs.
- Target bitrate = `w * h * fps * 0.05` (HEVC delivery bpp), rounded to
  nearest 0.5 Mbps.
- `MIN_SAVINGS_FRACTION = 0.20` — candidates that'd save <20 % are dropped.


A lot of incoming content is edited deliveries or 360 exports encoded at
3–5× the bitrate they need. A 6 GB 4K24 recap becomes 1.2 GB with no
visible quality loss. The detector runs off the same header read as
Phase 2b — we already have codec, dimensions, fps, bitrate.

- **Bloat score**: `bits_per_pixel_per_frame = bitrate / (w · h · fps)`.
  Thresholds (rough, tune on real data):
  - H.264 delivery: sane < 0.15, fat 0.15–0.30, obscene > 0.30
  - HEVC delivery: sane < 0.08, fat 0.08–0.15, obscene > 0.15
- **Source / preserve allowlist** (never flag as bloat — these are edit
  sources, not deliveries):
  - Filename prefixes: `DJI_`, `GX`, `GH`, `GOPR`, `MAH`, `MVI_`, `C0`,
    `LRV_`, `PRO_`, `DSC_`, date-stamped `VID_`/`IMG_`
  - Extensions: `.insv`, `.insp`, `.lrv`, `.lrf`, `.mts`, `.dng`, `.braw`
  - Codec `prores`, `dnxhd`, `cineform`, `ffv1`, anything RAW
  - **All Insta360 content**, including reframed/exported `.mp4` at
    5760×2880 or 7680×3840 — these are re-edit sources, not deliveries.
    Equirectangular aspect ratios (2:1) at high bitrate are the tell.
  - Anything in a folder named `*raw*`, `*source*`, `*edit*`, `*project*`.
- **Confirm-before-transcode UI** (same sidecar that hosts the gap-fill UI):
  - Groups candidates by parent folder — "Antarctica: 5 files, 37 GB,
    would save 24 GB" is one click, not 5.
  - Per-group: preview thumb strip, current vs. target codec/bitrate,
    estimated output size, estimated transcode time.
  - Dry-run / sample: transcode first 10 s, show before/after side-by-side.
- **Transcode worker** (pattern-matches the other enrichment workers):
  - Apple hardware: `hevc_videotoolbox -tag:v hvc1 -b:v <target> -c:a copy`.
  - Target bitrate from a table keyed by `(resolution, fps)`.
  - Idempotent: output to `.optimized.mp4` sidecar first, verify duration
    and stream count match, only then atomic-replace original.
  - Keeps a `pre_transcode_sha256` + original size in the catalog row so
    we always know what was replaced.
  - Skip if estimated savings < 20 % (not worth the CPU and quality hit).
- **Always-keep rules**: never transcode if the file is already on the
  camera-native allowlist, already below the "sane" threshold, or is
  flagged `preserve=true` in the XMP sidecar (user opt-out per file).

**Done when**: running the detector across a fresh 1 TB inbox produces a
grouped report, batch-confirm reclaims ≥ 30 % of bloat-candidate GB, and
every transcoded file retains its catalog identity (same asset row, same
face matches, same album memberships).

## Phase 3 — Proxy-first AI enrichment (3–5 days)

**Whisper + captioner + CLIP all on proxies, never originals.**

- Queue table in Postgres keyed by `(checksum, worker, version)`.
- Whisper worker on Mac: `whisper.cpp` Metal, `.srt` sidecar + Immich
  description update.
- Captioner worker on Mac: `moondream2` (fast) or BLIP-2 (richer), appends
  `AI: …` to description for CLIP-adjacent text search.
- Both idempotent, resumable, and guarded against ever opening originals.

**Done when**: searching for a phrase spoken in a video finds the video.

## Phase 4 — Event clustering (2 days)

**Trips become albums automatically.**

- Nightly cron (Mac or Syno).
- DBSCAN on `(time, lat, lon)` — tune ε to personal travel patterns.
- Nominatim lookup on cluster centroid for human-readable name.
- Create/update album via Immich API.

**Done when**: last year's travel photos retroactively organise into sensibly
named albums without manual work.

## Phase 5 — Metadata gap-fill UI (2–3 days)

**Small sidecar web app; not a fork of Immich.**

- Route on sidecar: `/gap` shows assets missing GPS or timestamp, grouped.
- Thumb grid + suggested location from nearest timestamped neighbour.
- Apply-to-group button writes via Immich REST + XMP sidecar.

**Done when**: filling in location for a 200-photo trip takes one click, not 200.

## Phase 6 — Ghost assets (1–2 days)

**Offline drives stay searchable.**

- State machine on assets: `online / offline / resurrecting`.
- Open-original returns friendly error when offline.
- Re-mount detection auto-resurrects without re-scanning.

**Done when**: unplugging the archive drive still lets me search, browse
thumbs, open transcripts and face matches — just not the original file.

## Phase 7 — Quality of life (ongoing)

### External library matching — planned

Four-tool bundle for finding "is this file already in Immich?" from any
external disk, and seeding Immich people from Apple Photos. Full build spec
in [`/PLAN.md`](../PLAN.md) at the repo root (short-horizon; folded back
here once shipped).

- `immy snapshot` — dump Immich library index (filename, size, SHA1,
  optional CLIP embeddings) to a portable SQLite file. Foundation for all
  three other tools.
- `immy find-duplicates <path>` — scan a disk/folder, report exact matches
  (filename + size + optional hash) against the snapshot. Three tiers:
  `exact` / `likely` / `name-only`. Gives you "safe to delete" vs "needs
  ingest" lists for backup drives.
- `immy find-similar <path>` — CLIP near-dup finder for files that aren't
  byte-identical but are the same photo (re-export, edit, crop). Deferred
  until 1 + 2 have been in use for a while.
- `immy import-apple-people` — read `Photos.sqlite`, create Immich Person
  rows with Apple names, attach face embeddings via filename-matched asset
  overlap. Assumes `--apply` against Immich REST + direct `asset_faces`
  updates.

Build order: `snapshot` → `find-duplicates` → `import-apple-people` →
`find-similar`. First two share the most infra; the Apple importer jumps
the queue over similarity search because tagging history is high-value and
reuses the same snapshot.

### Other QoL items

- Export-to-edit: given a date range or album, symlink-package into a working
  dir on the Mac.
- Hyper Backup job: originals + `pg_dump` of Immich DB to external drive / C2.

## What's custom vs stock

| Custom (in our sidecar) | Stock Immich |
|---|---|
| Inbox watcher + normaliser | Web UI, mobile apps, timeline |
| Preview extractor (RAW / HEIC / LRV / LRF) | EXIF + map + places |
| Bloat detector + batch re-encode UI | — |
| 360 + DJI telemetry | pHash dedup |
| Whisper + captioner | CLIP smart search |
| Event clustering | InsightFace face recognition |
| Gap-fill UI | API, Postgres schema, pgvector |
| Ghost-asset state machine | External libraries, stacks |
| `osxphotos` + `icloudpd` bridges | iOS auto-upload |

## Risks

- **Immich API drift.** Pin a version, track release notes. The API surface
  we touch is small (3 endpoints), so migrations should be cheap.
- **`immich-ml-metal` is unofficial.** If it goes stale, fall back to Immich
  stock ML on Mac (ONNX Runtime + CoreML execution provider) or Syno CPU.
- **ML cost on backlog.** Cap initial enrichment to last N months, backfill
  older content over weeks.
- **`osxphotos` fragility.** Apple breaks the Photos.app schema every couple
  of macOS majors; pin `osxphotos` to a known-good version and test on
  upgrades.
- **Postgres on Syno HDD.** Don't. Postgres must live on NVMe.

## Out of scope (for now)

- Multi-user / family sharing.
- Web-based video editor.
- Live transcode streaming (proxies + download only).
- Cloud-hosted public galleries.

## Open questions

- Do we want to contribute event clustering upstream to Immich, or keep it
  in our sidecar? Upstream is better long-term but slower.
- `buffalo_l` vs `buffalo_s` — start with `l` and downgrade only if the Mac
  is a bottleneck (unlikely).
- Do we include Apple Photos people names as Immich person labels
  automatically, or require manual approval? Propose: auto-suggest, manual
  confirm, per cluster.
