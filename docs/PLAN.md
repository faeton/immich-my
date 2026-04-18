# Build plan

Phased so each phase is independently useful. Stock Immich first, sidecar
bolted on in layers. Each phase has a "done when" so we don't drift.

Rough effort estimates are evening-hours for a single person.

## Phase 0 — Base stack ✅ done

**Stock Immich on the Syno, no custom code.** Running under Container Manager,
docker project `fnim`, all state under `/volume1/faeton-immi/`. Full as-built
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

**Still to close out before moving on** (tracked in TESTING.md Phase 0):
- Create the External Library in Admin → Libraries, point at
  `/mnt/external/originals`, run a scan against an empty tree.
- iOS Immich app login over Tailscale + one end-to-end upload.
- First `pg_dump` + tarball of `library/` to an external destination, so a
  lost DB doesn't lose face labels.

## Phase 1 — Mac as burst ML node (½ day)

**Offload heavy ML to the MacBook.**

Operating constraint: **Mac is mobile and often on bad uplink** (5–10 Mbps
up, frequently captive-portal / hotel / tethered, sometimes off-tailnet).
Plan the ML path around this — the Mac is an *opportunistic* worker, not
a reliable one. NAS-side CPU ML must always be able to drain the queue
alone; Mac-side is a speedup when available.

- OrbStack or Docker Desktop on the Mac.
- Run `immich-ml-metal` container on the Mac, exposed over Tailscale
  (not LAN — we may not share a LAN with the NAS most days).
- Set Immich's `MACHINE_LEARNING_URL` to Mac primary with Syno stock ML
  as fallback via `immich_ml_balancer`. Short timeout (2–3 s) so Mac-
  unreachable doesn't stall the queue.
- ML traffic is proxy/preview-sized (thumbnails + embeddings), not
  originals — budget in the tens of KB per asset, not MB. A 50k backfill
  over a 5 Mbps link is still hours of pure transfer; factor that into
  "done when" below rather than assuming LAN speeds.
- All workers idempotent on `(checksum, worker, version)` so a flapping
  link or a closed laptop lid never loses progress.

**Done when**: with Mac on stable Tailscale + mains power, a 50k backfill
completes in ≈ 1 h; with Mac offline, the same backfill still drains on
Syno CPU (slower, but nothing stuck). Mid-flight disconnect never leaves
a job wedged > 10 min.

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
| **2a.2** | Date-authority resolver (`exif > companion > filename > mtime`) + folder-median clock-drift rule (MEDIUM tier, flags files >24 h from median with source + delta in the reason line) + MEDIUM-confidence y/n prompter + `--yes-medium` flag. Per-tier dedup so a MEDIUM finding survives even when a HIGH rule also claims the same XMP field (user accepts MEDIUM only after HIGH has converged). `--yes-high` deferred — HIGH today is unconditionally applied under `--write`; the flag only becomes meaningful once watcher mode (2a.6) introduces HIGH prompts. | `clock-drift-simple` fixture (3 coherent + 1 four-day outlier) surfaces one MEDIUM prompt with human-readable reason. `--yes-medium` → exiftool writes XMP:DateTimeOriginal = folder median; re-audit clean. Interactive y/n/skip honoured. | ✅ shipped |
| **2a.3** | MEDIUM auto-propose of missing "obvious" tags for folders where the notes `tags:` list was hand-edited. Rule = `tag-suggest-missing`: compares existing tags to what the scaffold *would* produce from current EXIF/filenames, proposes any tag whose category (everything before the last `/`) is entirely absent from the user's list. Opt-out via `tag_suggestions: off` front-matter. Introduces a new `write_notes` action: accepted patches merge into notes front-matter and cascade to XMP via `trip-tags-from-notes` on the next apply pass. Immich-round-trip assertion of the full hierarchy lands with 2a.4 (`promote`). | `tag-suggest-missing/` fixture (Nikon JPGs + TRIP.md with only `Events/CustomEventLabel`) surfaces one MEDIUM prompt. `--yes-medium` merges `Gear/Camera/...` + `Source/DSC` into notes, cascade re-fires `trip-tags-from-notes`, XMP sidecar carries the full set. Re-audit clean. | ✅ shipped |
| **2a.4** | `immy promote` (aliases `push`, `pub`): rsync trip folder to `originals_root`, trigger Immich `POST /api/libraries/:id/scan`, then `POST /api/stacks` per `.insv` ↔ `.lrv` pair (`.lrv` primary). Config from `~/.immy/config.yml` (or `$IMMY_CONFIG` / `--config`). `.audit/` excluded from rsync. Guard rail: refuses with exit 1 if HIGH findings are pending (override via `--force`). `--dry-run` skips all writes and API calls. Immich section of config is optional — missing creds degrade to rsync-only. | `promote --dry-run` performs zero writes and zero API calls. `promote` on an audited trip rsyncs, calls scan once, and calls `/api/stacks` once per Insta360 pair with the `.lrv` asset ID as primary. Re-running is a no-op on disk; Immich gets re-notified (cheap). | ✅ shipped |
| **2a.5** | Remaining LOW rules: ✅ interactive `trip-timezone` prompt (pre-audit, mirrors the coords prompt; validates via `zoneinfo`, writes IANA zone to notes, cascades through `trip-timezone` HIGH); ✅ `export-date-trap` (LOW `note` flag when `ModifyDate` is present but `DateTimeOriginal`/`CreateDate` are absent — files that would sort at export time on the Immich timeline). Pending: bloat-candidate flag (feeds Phase 2c), MakerNote cleanup, `geotag-from-gpx`, place-name → coords geocoding. | Two real trips each go through with <10 % LOW-confidence prompts. | partial (coords + tz prompts + export-date-trap done; GPX/geocode/bloat/MakerNote pending) |
| **2a.6** | Watcher mode: `launchd` plist, debounced `watchdog` on `~/Documents/Incoming/`, non-interactive `--yes-high`. | Drop a folder in Incoming, walk away; return to either clean promotion or a `NEEDS_REVIEW` file listing open questions. | pending |
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

## Phase 2c — Bloat detector + batch re-encode (1–2 days)

**Find files that are uselessly huge, confirm in groups, transcode.**

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

- Cross-device near-dup reporter (Immich pHash misses some camera+phone pairs).
- Export-to-edit: given a date range or album, symlink-package into a working
  dir on the Mac.
- Hyper Backup job: originals + `pg_dump` of Immich DB to external drive / C2.
- Face-name pre-seed from Apple Photos people (via `osxphotos` JSON).

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
