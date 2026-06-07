# Roadmap

Current planning snapshot: 2026-05-19.

This file is the short, current roadmap.
`docs/archive/PLAN-2026-04-historical.md` remains the historical phase
narrative and acceptance criteria; `raw/TODO.md` remains the private
scratchpad.

## Now

### Stabilize Current Tree

- Keep `uv run pytest` green.
- Preserve the current promote cancellation contract: Ctrl-C during rsync exits
  130 and does not continue into scan, stack, derivative push, offline sync, or
  album sync.
- Align docs with shipped behavior after every feature batch, especially Phase Y
  direct-DB ingest.

### Local Immich Triage

Spec: [raw/LOCAL-IMMICH-TRIAGE.md](../raw/LOCAL-IMMICH-TRIAGE.md) (planned).

Goal: replay existing offline caches into a local Immich instance, review and
prune trips locally, then replay the same cached work to vv-nas later.

Work:
- Add per-target offline sync state: `synced.local`, `synced.vv`.
- Add `immy sync-offline --target {vv,local}`; default remains `vv`.
- Add `tools/immich-local/docker-compose.yml`.
- Add `tools/immich-local/link-derivatives.sh` to symlink pre-baked derivatives
  into the local Immich upload layout without copying tens of GB.
- Document `~/.immy/config.local.yml`.

### Operational Preflight

Add `immy doctor`:
- config path and parsed sections
- required binaries: `exiftool`, `ffmpeg`, `ffprobe`, `vips`
- Immich API reachability
- Postgres reachability
- configured library exists and has import paths
- media host/container roots look plausible
- `smart_search.embedding` dimension matches configured CLIP model
- direct-write schema columns exist

## Next

### Trip Status Command

Add `immy status <trip>`:
- audit HIGH/MEDIUM pending count
- process marker state
- journal phase counts
- offline entries pending/synced by target
- staged derivative files present/missing
- last heartbeat/progress file

### Docs Restructure

- Keep `README.md` concise: current capabilities, setup, and links.
- Keep `docs/archive/PLAN-2026-04-historical.md` as historical phase context.
- Keep this file as the active roadmap.
- Keep `docs/REVIEW-RECOMMENDATIONS.md` as the review snapshot that motivated
  this roadmap.

### Cluster Pruning

`immy cluster` currently only adds assets to cluster albums. Add stale-member
removal for albums marked with `immy-cluster:<key>`.

Likely storage:
- per-asset prior cluster key in journal, or
- a small immy-owned table if this becomes cross-trip/global state.

## Later

### Apple Photos People Apply

Spec: [raw/PLAN.md](../raw/PLAN.md) — external-library matching.
`snapshot` + `find-duplicates` shipped; `find-similar` deferred (see below);
`apple-people --apply` pending a good match rate from a fresh snapshot.

The dry-run importer exists. The apply path should wait until a fresh snapshot
shows a good match rate.

Work:
- create Immich people through the API
- attach only high-confidence face matches
- emit an audit report before writing rows

### CLIP Near-Duplicate Search

Build `immy find-similar` after exact duplicate reports have been used on real
backup disks long enough to prove the remaining need.

Likely flow:
- extend `immy snapshot --with-embeddings`
- embed candidate files locally
- cosine rank top matches above threshold
- report probable edits/re-exports separately from exact duplicates

### Metadata Gap-Fill UI

Small sidecar UI for grouped missing GPS/timestamp repairs:
- map picker
- thumbnail grid
- apply-to-group writes both XMP and Immich metadata

### Ghost / Offline Assets

Keep offline originals searchable and browsable:
- status transitions for mounted/unmounted volumes
- friendly "original unavailable" errors
- automatic resurrection on remount

