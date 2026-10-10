# immich-my

A personal media catalog and ingest sidecar built around
[Immich](https://immich.app).

The project is aimed at a photo/video workflow that mixes:
- cameras and SD-card imports
- phone media
- DJI sidecars / telemetry
- Insta360 pairs
- trip-based curation
- remote or occasionally-offline originals

Immich stays upstream and unmodified. The custom logic lives beside it:
- `immy audit` fixes metadata with XMP sidecars instead of rewriting originals
- `immy process` computes derivatives, CLIP, and faces on the Mac
- `immy promote` uploads a curated trip into an Immich external library

This repo is both:
- the code for the `immy` ingest tool
- the design / operations docs for the broader setup

## CLI

The main operational interface is `immy`:
- `audit` reads media, proposes metadata fixes, and writes XMP sidecars
- `bloat` finds oversized video sources and helps transcode them
- `process` computes derivatives, CLIP, and faces, then inserts directly into Immich Postgres
- `promote` uploads a curated trip into the external library and syncs the album
- `cluster` groups geo-dated assets into events and auto-creates Immich albums
- `takeout redate` repairs Google Takeout capture dates (placeholder or UTC-clock) from the JSON companions, via XMP sidecars, and stacks Takeout copies under their originals; see `docs/TAKEOUT.md`
- `trips` finds trips in the library's day-by-day geography (homes, regions, stopovers, flipped-GPS repair, districts rolled up to their city) and keeps one dated album per trip, plus optional `Trips/<year>/…` tags for nesting (small trips: tags only); see `docs/TRIPS.md`
- `sidecars check` repairs registered XMP sidecars that contradict their originals (a GPS hemisphere lost, a video's UTC clock read as local), dates no-GPS videos Immich shows on the UTC clock from the shots around them, and splits stem-shared sidecars per file; the last stage of the ingest, see `docs/SIDECARS.md`
- `srt` harvests DJI `.SRT` telemetry → GPX/JSON tracks, durable GPS, reverse-geocode
- `tags sync` pushes a trip's notes tags (gear/camera, event, source) to Immich's native Tag API — the only channel that reaches video assets, which never read XMP
- `tags camera` backfills the blank "Camera" field for DJI MP4s from the notes gear tag
- `snapshot` dumps the Immich library index to a portable SQLite file
- `similar <photo>` finds the library assets that look like a photo (image→image search
  on Immich's own CLIP index; ≥0.95 = same frame, 0.85–0.95 = same subject)
- `find-duplicates` scans any disk/folder and reports what's already in Immich
- `photos diff` lists the Photos.app assets missing from Immich (read-only, no iCloud login)
- `photos pull` exports those UUIDs with osxphotos, re-exports anything incomplete through
  Photos.app AppleScript (iCloud-only originals, e.g. a Live Photo's video), and delivers
  batches to n5 `staging/photos/ready/` with a per-UUID retry ledger

Typical development commands:

```sh
cd immy
uv sync
uv run immy --help
uv run immy audit tests/fixtures/dji-srt-pair
uv run pytest
```

## Batch wrappers

Non-interactive batch wrappers under `tools/` drive `immy` over every
trip under `~/Media/Trips`. They preflight, log to `~/.immy/*-logs/`,
and skip work already done:

- `tools/process-all-trips.sh` — runs the full `immy process` pipeline
  (derivatives + CLIP + faces + transcripts + captions). Defaults to
  `--offline` (caches to `.audit/offline/`); `--online` writes straight
  to Postgres, `--sync` replays a cached run to the DB, `--status`
  reports only. Requires LM Studio + Gemma 4 for captions.
- `tools/promote-all-trips.sh` — rsyncs originals + derivatives to the
  NAS and triggers the Immich-side steps for every pending trip. Uses
  `rsync --partial --append --inplace`, so a dropped connection on a
  multi-GB video resumes on rerun rather than restarting. Skips trips
  already logged as promoted.
- `tools/overnight.py` — pipelined wrapper that overlaps a single
  `immy process` stream with a parallel `immy promote` upload pool, so
  the laptop transcodes trip B while trip A uploads. `--captions` runs
  the VLM captioner (and preflights LM Studio first — it refuses to start
  if the endpoint is down or the model isn't downloaded, rather than
  silently captioning nothing); by default it fills only never-captioned
  assets across a model swap (`--recaption-all` forces a full re-caption).
  `--reprocess` revisits every trip to backfill missing phases;
  `--no-upload` / `--no-process` split the CPU and network sides;
  `--status` reports only. A live dashboard shows per-trip progress and
  truthful caption generated/failed counts.
- `tools/verify-transcripts.py` — dual-engine transcript quality check:
  re-transcribes every `.srt` sidecar with an independent engine (GigaAM-v3
  for Russian on CPU, Whisper for English on GPU), scores word-level
  agreement, sends low scorers to an LM Studio judge, and (after human
  review of the verdicts) drops hallucination-only sidecars. See
  [docs/TRANSCRIPTS.md](docs/TRANSCRIPTS.md).
- `tools/audit-journal.py` — true per-trip enrichment coverage keyed the
  way the pipeline keys it (path-hash journal), with stale-key pruning.

Local prerequisites:
- `exiftool`
- `ffmpeg` / `ffprobe`
- `vips` if you want derivative generation

On macOS:

```sh
brew install exiftool ffmpeg vips
```

## What works today

- Stock Immich 3.3.1 deployment on the n5 NAS (TrueNAS SCALE); immy is tested
  against 3.3.1 (see `immy/src/immy/data/immich_schema.json`)
- Trip-folder metadata audit and XMP sidecar writes
- GPS / timezone / tag inference for trips
- Direct-to-Postgres ingest for curated trips
- Thumbnail / preview / encoded-video derivative staging
- CLIP embeddings and face embeddings during ingest
- Album sync on promote
- Event clustering into auto-named albums (`immy cluster`)
- Dated travel albums across the whole library (`immy trips`, see `docs/TRIPS.md`)
- DJI drone telemetry: GPX/JSON tracks, durable locked GPS, library-matched
  reverse-geocode (`immy srt`, see `docs/TELEMETRY.md`)
- Portable Immich library snapshot + external-disk duplicate scan
  (`immy snapshot`, `immy find-duplicates`)
- Apple Photos → Immich bridge (`immy photos diff`, `immy photos pull`, n5
  `deploy/n5/photos-ingest.sh`); the 4,634-asset backlog is in

## Local Setup

This public repo uses placeholder names in docs and examples. Real local values
stay out of git.

Files and roles:
- [`.env.example`](.env.example): public-safe placeholder names used in docs
- `.env`: your real local values, gitignored
- `.immy/config.yml`: your real local `immy` config, gitignored
- `~/.immy/config.yml`: also supported; `immy` reads this by default

Recommended local flow:

```sh
cd /path/to/immich-my
cp .env.example .env
mkdir -p .immy
cp /path/to/your/immy-config.yml .immy/config.yml
source .env
```

If you use a repo-local config, set:

```sh
export IMMY_CONFIG=/absolute/path/to/immich-my/.immy/config.yml
```

Notes:
- `.env` is for your shell and local placeholders; `immy` does not auto-read it
- `immy` runtime config comes from `--config`, `$IMMY_CONFIG`, or `~/.immy/config.yml`
- `.env` and `.immy/` are gitignored

## Public Placeholders

Docs may use `${PLACEHOLDER}` values such as `${IMMICH_URL}` or
`${DEPLOY_ROOT}`. These are documentation placeholders only. They make the
public docs readable without exposing the original private hostnames, paths, or
credentials from the live setup.

## Why this exists

Immich is the strongest open-source base available today — fast UI, mobile
backup, CLIP search, face recognition, pHash duplicate detection, active weekly
releases. But it is missing pieces for this workflow:

- No Whisper transcripts, no BLIP/LLaVA captions feeding search.
- No automatic event/trip grouping by time + location.
- No 360 (`.insv/.insp/.lrv`) or DJI telemetry handling.
- No Apple Photos / Photos.app library puller.
- No metadata-gap bulk-fill UI ("assign this location to all 80 files from this day").
- No "ghost asset" support for originals that live on drives you unplug.

Everything above is built as a **sidecar** that speaks to Immich over its public
REST API. No forking. Upgrades stay clean.

## Hardware snapshot

- **Storage + web + DB** → n5 (TrueNAS SCALE, ZFS): HDD pool `tank` for
  originals, NVMe pool `flash` for immy state and derivatives. Immich 3.3.1 runs
  as the TrueNAS `ix-immich` app; immy's NAS runner lives under `/mnt/flash/immy`
  (see [immy/deploy/n5/README.md](immy/deploy/n5/README.md)). A Synology (vv)
  is the cold backup target, not the primary (it was the primary before the
  2026 move to n5).
- **Compute** → MacBook Apple Silicon with OrbStack or Docker Desktop running
  `immich-ml-metal` + the curator sidecar. Replaceable with a Mac mini / N100
  later without touching the design.

## Documentation map

Docs grouped by role:

- **Entry** — this README.
- **Design** — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/SIDECAR.md](docs/SIDECAR.md).
- **Operations** — [docs/DEPLOY.md](docs/DEPLOY.md), [docs/OFFLINE-RUNBOOK.md](docs/OFFLINE-RUNBOOK.md).
- **Current work** — [docs/ROADMAP.md](docs/ROADMAP.md), [docs/REVIEW-RECOMMENDATIONS.md](docs/REVIEW-RECOMMENDATIONS.md).
- **Feature guides** — [docs/CAPTIONS.md](docs/CAPTIONS.md), [docs/TRANSCRIPTS.md](docs/TRANSCRIPTS.md), [docs/TELEMETRY.md](docs/TELEMETRY.md), [docs/MATCH.md](docs/MATCH.md), [docs/LANDSCAPE.md](docs/LANDSCAPE.md).
- **Reference** — [docs/IMMICH-INGEST.md](docs/IMMICH-INGEST.md), [docs/archive/](docs/archive/) (historical phase plan).
- **Personal backlog** — [raw/](raw/).

## What's in this folder

| File | What it covers |
|---|---|
| [docs/LANDSCAPE.md](docs/LANDSCAPE.md) | Survey of open-source alternatives and why Immich won |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Technical design: tiered storage, mount adapters, pipelines, queues |
| [docs/SIDECAR.md](docs/SIDECAR.md) | Sidecar internals: DB choice, queue schema, worker-harness contract, process layout |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Current work order and forward roadmap |
| [docs/archive/PLAN-2026-04-historical.md](docs/archive/PLAN-2026-04-historical.md) | Historical phased build plan (Phase 0/1/1b/Y/2a), kept for context |
| [docs/REVIEW-RECOMMENDATIONS.md](docs/REVIEW-RECOMMENDATIONS.md) | Current engineering review, fixes, suggestions, and commit plan |
| [docs/CAPTIONS.md](docs/CAPTIONS.md) | VLM captioner — supported backends, config, per-image cost table |
| [docs/MATCH.md](docs/MATCH.md) | `immy match` — place an inbound dump against the library (offline dedup + trip placement) |
| [docs/OFFLINE-RUNBOOK.md](docs/OFFLINE-RUNBOOK.md) | Step-by-step offline runbook — drive `immy` with LM Studio + a local VLM, no internet |
| [docs/DEPLOY.md](docs/DEPLOY.md) | As-deployed operating manual: paths, compose, onboarding choices |
| [docs/TESTING.md](docs/TESTING.md) | Acceptance tests per phase + ad-hoc smoke checks |

## Status

- **Phase 0 — Base stack**: done. Stock Immich (now 3.3.1) running on n5
  (TrueNAS SCALE, `ix-immich` app), reached over Tailscale. Originally built on a
  Synology DS923+ (docker project `${COMPOSE_PROJECT}`, data under
  `${DEPLOY_ROOT}`); that layout is kept in the DEPLOY.md history. Public docs use placeholders from
  `.env.example`; details in [docs/DEPLOY.md](docs/DEPLOY.md).
- **Phase Y — direct-to-Immich-DB pre-processing**: done. `immy process →
  promote` lands asset + EXIF + derivatives (thumbnail/preview/encoded_video)
  + CLIP + faces straight into Postgres without touching Immich's scan
  pipeline. InsightFace `buffalo_l` runs on the ANE via onnxruntime-CoreML;
  MLX-CLIP and Apple Vision cover the rest. `immich-accelerator` removed
  2026-04-20 — `immy` is the sole ingestion path. See
  [docs/archive/PLAN-2026-04-historical.md](docs/archive/PLAN-2026-04-historical.md)
  for the Y.1–Y.6 ladder.

## Current capabilities

- Whisper transcripts via `immy process --with-transcripts` (mlx-whisper on
  Apple Silicon, large-v3 by default). Writes `<stem>.<lang>.srt` next to
  the source and an excerpt into `asset_exif.description` so Immich search
  hits spoken words. Cheap guards (sidecar cache → DJI make denylist →
  ffprobe audio-stream check → ffmpeg volume/silence detection) skip Whisper
  on footage that can't produce meaningful speech. Insta360 clips are probed
  normally because newer X-series cameras can record useful audio. For biased
  auto-detect in multilingual corpora, set `ml.whisper_prompt` in
  `~/.immy/config.yml` (e.g. `"English, Russian, Ukrainian."`) or export
  `IMMY_WHISPER_PROMPT` — it's passed to Whisper as `initial_prompt`.

- Image captions via `immy process --with-captions`. Any OpenAI-compat
  `/chat/completions` endpoint — LM Studio (default, free, local),
  Ollama, OpenAI, Anthropic, Gemini, OpenRouter, Groq. Captions land in
  `asset_exif.description` with an `AI: ` prefix so they never clobber
  user-typed text. Per-image cost table + config recipes in
  [docs/CAPTIONS.md](docs/CAPTIONS.md) — expect ~$0.5–2 per 1 000
  photos on the cheap cloud tiers, $0 locally (overnight on Apple
  Silicon).

- Event clustering via `immy cluster` — sweep-based `(time, lat, lon)`
  grouping, auto-named albums from Immich's city/country, idempotent via a
  marker line in each album's description.

- Travel albums via `immy trips` — day-track majority vote, configurable
  home stays, region-aware runs with stopover folding, sign-flipped GPS
  dropped, one dated album per trip; see `docs/TRIPS.md`.

- External-disk matching. `immy snapshot` dumps the Immich library index
  (filename, size, SHA1, taken-at) into a portable SQLite file. On any
  other machine, `immy find-duplicates <path>` walks a tree and reports
  each file as `exact` / `likely` / `name-only` / `no-match` against the
  snapshot — tells you which backup drive content is already in Immich
  (safe to delete) vs which is a candidate for ingest. Default mode
  hashes only on name+size hits; `--thorough` catches pure renames.

- Apple Photos bridge. On the Mac, `immy photos diff` reads Photos.sqlite
  and the Immich snapshot and lists the UUIDs Immich lacks. `immy photos pull`
  exports them in batches with `osxphotos` (PhotoKit downloads iCloud-only
  originals). Any asset that comes out incomplete gets one more try through
  Photos.app's own AppleScript `export … with using originals`, which fetches
  what PhotoKit reports missing (the original video of an edited Live Photo).
  Half an asset is never delivered: whatever is still incomplete is dropped
  from the batch and retried next run. Batches are rsynced to n5, checked
  file by file, then published to `staging/photos/ready/`, where
  `deploy/n5/photos-ingest.sh` dedups them against the library before
  promote. Design and history: `todo/PHOTOS-BRIDGE-REVIEW.md`.

## Known gaps

- local Immich triage of offline-cached trips
- metadata gap-fill web UI
- ghost/offline asset handling

Current work order lives in [docs/ROADMAP.md](docs/ROADMAP.md). Historical
phased planning lives in
[docs/archive/PLAN-2026-04-historical.md](docs/archive/PLAN-2026-04-historical.md).

## License / Publishing Note

This repo is intended to be publishable. Real deployment-specific values should
live only in local ignored files such as `.env` and `.immy/config.yml`, not in
tracked docs or code examples.
