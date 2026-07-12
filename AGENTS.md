# AGENTS.md

Orientation for AI agents working in this repo. Keep it short; deep detail lives in `docs/` and `raw/`.

## What this is
A personal media catalog + **ingest sidecar** around [Immich](https://immich.app). Immich stays
upstream and unmodified; the custom logic is the `immy` CLI. Sources (cameras, phone, DJI, Insta360)
are curated into trips and enriched (metadata, derivatives, CLIP, captions, transcripts).

## Where things live
- `immy/` — the tool. Code in `immy/src/immy/`, tests in `immy/tests/`. Python, `uv`, hatchling.
- `immy/deploy/n5/` — running immy as a container on the N5 NAS (Immich's docker network).
- `docs/` — published design/ops docs (ARCHITECTURE, DEPLOY, CAPTIONS, TRANSCRIPTS, OFFLINE-RUNBOOK…).
- `raw/` — personal specs + hardware notes. **gitignored** (CONSOLIDATION-PIPELINE, IMMY-ON-N5, PRIMARY-SWAP…).
- `README.md` — human overview. `CHANGELOG.md` — dated findings + changes, newest first.

## How it works (`immy` subcommands)
`audit` (metadata fixes via XMP sidecars) · `process` (derivatives + CLIP + faces + captions +
transcripts → inserts/writes Immich Postgres) · `promote` (upload a trip to an external library) ·
`cluster` (geo-date albums) · `srt` (DJI .SRT telemetry → GPX/JSON tracks, durable locked GPS,
reverse-geocode from Immich's geodata; see `docs/TELEMETRY.md`) ·
`tags sync` (push notes `tags:` — Gear/Camera/drone model, event, source — to Immich's native
Tag API; the only channel that reaches video assets, which never read XMP; see `docs/TELEMETRY.md`) ·
`tags camera` (backfill the blank Camera field for DJI MP4s from the notes gear tag; see `docs/TELEMETRY.md`) ·
`snapshot` / `find-duplicates` / `repair-thumbs`.

Two run targets: the **Mac** (MLX, default) and the **N5 NAS** (HTTP backends — Ollama captions,
Qwen-ASR, Immich's own CLIP). The same code runs both; backend is config-selected.

## How to work here
```sh
cd immy && uv sync
uv run immy --help
uv run pytest            # run from immy/ (cwd matters for venv/PIL)
```
- **Originals are immutable** — never rewrite source files; metadata goes to XMP sidecars / the DB.
- **Keep the Mac path byte-identical** when adding NAS behavior (new config defaults to the old path).
- **Git**: work, commit, and merge on `main` directly; branch only when explicitly asked.
- Captions are prefixed `AI: ` and DB-locked so an Immich metadata refresh can't clobber them.
- Commit messages with backticks: use `git commit -F <file>` (zsh eats backticks in `-m`).

## Deeper context
`docs/ARCHITECTURE.md` (design), `docs/DEPLOY.md` + `immy/deploy/n5/README.md` (NAS),
`raw/IMMY-ON-N5.md` (NAS port), `raw/CONSOLIDATION-PIPELINE.md` (incoming iCloud/Google import).
