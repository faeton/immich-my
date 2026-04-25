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
- `snapshot` dumps the Immich library index to a portable SQLite file
- `find-duplicates` scans any disk/folder and reports what's already in Immich

Typical development commands:

```sh
cd immy
uv sync
uv run immy --help
uv run immy audit tests/fixtures/dji-srt-pair
uv run pytest
```

## Batch wrappers

Two non-interactive batch scripts under `tools/` wrap `immy` for
overnight runs over every trip under `~/Media/Trips`. Both preflight,
log to `~/.immy/*-logs/`, and skip work already done:

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

Local prerequisites:
- `exiftool`
- `ffmpeg` / `ffprobe`
- `vips` if you want derivative generation

On macOS:

```sh
brew install exiftool ffmpeg vips
```

## What works today

- Stock Immich deployment on a Synology NAS
- Trip-folder metadata audit and XMP sidecar writes
- GPS / timezone / tag inference for trips
- Direct-to-Postgres ingest for curated trips
- Thumbnail / preview / encoded-video derivative staging
- CLIP embeddings and face embeddings during ingest
- Album sync on promote
- Event clustering into auto-named albums (`immy cluster`)
- Portable Immich library snapshot + external-disk duplicate scan
  (`immy snapshot`, `immy find-duplicates`)

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

- **Storage + web + DB** → Synology DS923+, Ryzen R1600 (2c/4t), 20 GB ECC RAM,
  HDD pool for originals, NVMe storage volume for derivatives, 1 GbE×2 (LAG or
  future 10 GbE via E10G22-T1-Mini).
- **Compute** → MacBook Apple Silicon with OrbStack or Docker Desktop running
  `immich-ml-metal` + the curator sidecar. Replaceable with a Mac mini / N100
  later without touching the design.

## What's in this folder

| File | What it covers |
|---|---|
| [docs/LANDSCAPE.md](docs/LANDSCAPE.md) | Survey of open-source alternatives and why Immich won |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Technical design: tiered storage, mount adapters, pipelines, queues |
| [docs/SIDECAR.md](docs/SIDECAR.md) | Sidecar internals: DB choice, queue schema, worker-harness contract, process layout |
| [docs/PLAN.md](docs/PLAN.md) | Phased build plan, milestones, what's custom vs stock |
| [docs/CAPTIONS.md](docs/CAPTIONS.md) | VLM captioner — supported backends, config, per-image cost table |
| [docs/OFFLINE-RUNBOOK.md](docs/OFFLINE-RUNBOOK.md) | Step-by-step offline runbook — drive `immy` with LM Studio + a local VLM, no internet |
| [docs/DEPLOY.md](docs/DEPLOY.md) | As-deployed operating manual: paths, compose, onboarding choices |
| [docs/TESTING.md](docs/TESTING.md) | Acceptance tests per phase + ad-hoc smoke checks |

## Status

- **Phase 0 — Base stack**: done. Stock Immich running on the DS923+ under
  Container Manager, docker project `${COMPOSE_PROJECT}`, data under
  `${DEPLOY_ROOT}`, reached over Tailscale. Public docs use placeholders from
  `.env.example`; details in [docs/DEPLOY.md](docs/DEPLOY.md).
- **Phase Y — direct-to-Immich-DB pre-processing**: done. `immy process →
  promote` lands asset + EXIF + derivatives (thumbnail/preview/encoded_video)
  + CLIP + faces straight into Postgres without touching Immich's scan
  pipeline. InsightFace `buffalo_l` runs on the ANE via onnxruntime-CoreML;
  MLX-CLIP and Apple Vision cover the rest. `immich-accelerator` removed
  2026-04-20 — `immy` is the sole ingestion path. See
  [docs/PLAN.md](docs/PLAN.md) for the Y.1–Y.6 ladder.

## TODO

Shipped since last README update:
- Whisper transcripts via `immy process --with-transcripts` (mlx-whisper on
  Apple Silicon, large-v3 by default). Writes `<stem>.<lang>.srt` next to
  the source and an excerpt into `asset_exif.description` so Immich search
  hits spoken words. Four cheap guards (sidecar cache → EXIF-make denylist
  for DJI/Insta360 → ffprobe audio-stream check → ffmpeg volumedetect) skip
  Whisper on footage that can't produce meaningful speech. For biased
  auto-detect in multi-lingual corpora, set `ml.whisper_prompt` in
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

- External-disk matching. `immy snapshot` dumps the Immich library index
  (filename, size, SHA1, taken-at) into a portable SQLite file. On any
  other machine, `immy find-duplicates <path>` walks a tree and reports
  each file as `exact` / `likely` / `name-only` / `no-match` against the
  snapshot — tells you which backup drive content is already in Immich
  (safe to delete) vs which is a candidate for ingest. Default mode
  hashes only on name+size hits; `--thorough` catches pure renames.

Not shipped yet:
- CLIP-based near-duplicate search (`find-similar`)
- Apple Photos people-name seeding (`apple-people --apply`)
- metadata gap-fill web UI
- ghost/offline asset handling

Detailed phased planning lives in [docs/PLAN.md](docs/PLAN.md).

## License / Publishing Note

This repo is intended to be publishable. Real deployment-specific values should
live only in local ignored files such as `.env` and `.immy/config.yml`, not in
tracked docs or code examples.
