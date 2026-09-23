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

Shipped 2026-09-23 as `immy doctor` (`immy/src/immy/doctor.py`): config
sections, binaries + libvips, configured roots, ML backend coherence, Immich
API + library import paths, Postgres reachability, direct-write columns,
`smart_search` dimension vs `ml.clip_model`. Read-only; exits 1 on any
failure. Run it inside the container on n5 — the config's paths and hosts are
container-side.

## Next

### Trip Status Command

Shipped 2026-09-23 as `immy status <trip>` (`immy/src/immy/status.py`):
pending audit HIGH/MEDIUM (`--no-audit` skips the exiftool pass), process
marker, journal counts per worker (flags a worker split across model
versions), offline entries synced/pending, staged derivatives present/missing,
last heartbeat with pid liveness. `--json` for scripts. Per-target offline
state (`synced.local` / `synced.vv`) will show up once Local Immich Triage
adds it.

### Docs Restructure

- Keep `README.md` concise: current capabilities, setup, and links.
- Keep `docs/archive/PLAN-2026-04-historical.md` as historical phase context.
- Keep this file as the active roadmap.
- Keep `docs/REVIEW-RECOMMENDATIONS.md` as the review snapshot that motivated
  this roadmap.

### Cluster Pruning

Shipped 2026-09-23 as `immy cluster --apply --prune`. A ledger
(`cluster-ledger.json` under `state_root`, else `~/.immy/`) records which
asset ids immy assigned to each `immy-cluster:<key>` album; prune removes only
those that left the event, so photos added by hand are never touched. Without
`--prune` claims accumulate (union) so a later prune still finds them. A key
whose event vanished loses all its immy-assigned members; the album is kept.
First run after upgrade has no ledger and prunes nothing.

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

