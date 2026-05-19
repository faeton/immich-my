# Review Recommendations

Date: 2026-05-19

Scope reviewed:
- Public docs: `README.md`, `docs/ARCHITECTURE.md`, `docs/PLAN.md`, `docs/TESTING.md`
- Main code paths: `immy audit`, `immy process`, `immy promote`, offline replay, derivative push, captions, transcripts, duplicate snapshot tools
- Personal backlog context: `raw/TODO.md`, `raw/LOCAL-IMMICH-TRIAGE.md`, `raw/PLAN.md`

Test command run:

```sh
cd immy
uv run pytest
```

Current result after this cleanup pass: **330 passed**.

The project is already useful and unusually well-tested for a personal media
pipeline. The main risk now is not feature absence; it is drift between shipped
behavior, tests, docs, and local operating assumptions.

## Executive Priorities

1. **Keep the suite green.** The immediate stale-test/code drift has been
   corrected; preserve this as the first gate for the next feature batch.
2. **Finish the local/offline triage path.** The personal backlog has a clear
   design for local Immich replay, but `sync-offline` still has one global
   `synced` flag. That blocks safe replay to both local Immich and vv-nas.
3. **Keep docs aligned with shipped reality.** The stale Phase Y labels were
   corrected in this pass; keep `README.md`, `docs/PLAN.md`,
   `docs/TESTING.md`, and `docs/ROADMAP.md` in sync after each feature batch.
4. **Make direct DB writes easier to validate against Immich upgrades.** The
   direct-to-Postgres path is powerful but brittle by design. Add a cheap schema
   compatibility probe before writes so failures happen before media work.
5. **Separate operational tools from exploratory scripts.** `tools/` contains
   both production-ish wrappers and one-off maintenance scripts. Grouping or
   documenting safety levels would reduce accidental misuse.

## Fix First

### 1. Keep repaired test failures covered

The failures observed in the initial review were corrected in this pass:

| Area | Original symptom | Resolution |
|---|---|---|
| `clip.py` | Fake `mlx_clip` tests failed because `get_model()` now passes `hf_repo`. | Test fakes now accept `hf_repo`, matching the current constructor call. |
| `cli._LazyModule` | Monkeypatching `immy.cli.pg_mod.connect` failed because `_LazyModule` used `__slots__` and had no writable module attrs. | `_LazyModule` now forwards `__setattr__` and `__delattr__` to the loaded module. |
| derivative push tests | Tests patched `promote_mod.subprocess.run`, but `_rsync_derivatives()` now uses `_run_streaming()` / `Popen`. | Tests patch `promote_mod._run_streaming`. |
| album sync tests | Tests expected album create/update without PG. Current `_sync_album()` uses PG for authoritative asset IDs. | Tests now provide fake PG config/connection and keep the DB-backed contract. |
| transcripts | `is_denylisted_make("Insta360")` expected old behavior. Code intentionally lets Insta360 fall through to audio probing. | Test now matches the new policy. |

### 2. Treat Ctrl-C behavior as a promote invariant

The uncommitted promote change is directionally right: interrupting rsync should
not continue into offline sync, derivative push, scan, stacking, or album sync.

Recommended extra coverage:
- `rsync` returns code `20` in both streaming and buffered paths.
- User sends `KeyboardInterrupt` while `_push_derivatives()` is streaming.
- Heartbeat is cleared for explicit user cancellation but left behind for
  unexpected crashes where last-known progress is useful.

### 3. Add per-target offline sync state

`raw/LOCAL-IMMICH-TRIAGE.md` identifies the blocker accurately:

```yaml
synced: true
```

is no longer enough if cached entries replay into both local Immich and vv-nas.

Recommended shape:

```yaml
synced:
  local: true
  local_at: 1777012124
  vv: false
```

Implementation touch points:
- `immy/src/immy/offline.py`: write, skip predicate, mark-synced path
- `immy/src/immy/cli.py`: `sync-offline --target {vv,local}` with `vv` as default
- `immy/src/immy/promote.py`: offline drain should target `vv`
- tests: migrate legacy `synced: true` as `synced.vv: true`

## Documentation Fixes

### 1. Normalize status labels

Initial review found Phase Y status drift: the plan heading still described the
work as design-stage, while later rows said it had shipped; the testing table
also lagged behind the shipped CLIP, faces, video proxy, and accelerator-removal
slices.

Status: corrected in this pass. Remaining follow-up: add an explicit
maintenance contract section once `immy doctor` exists.

### 2. Split roadmap from shipped state

`README.md` has a good current surface summary, but its TODO section mixes:
- shipped-since-last-update notes
- not-shipped product ideas
- detailed feature descriptions

Status: corrected in this pass. `README.md` now points at
[ROADMAP.md](ROADMAP.md) for current work. Keep this structure:
- `Current Capabilities`
- `Known Gaps`
- `Recent Changes`
- link to this review or a future `docs/ROADMAP.md`

### 3. Document safety levels for `tools/`

Suggested labels:
- **Batch wrappers:** safe/idempotent entry points (`process-all-trips.sh`, `promote-all-trips.sh`)
- **Maintenance scripts:** targeted cleanup (`merge-trip-folders.py`, `purge-bad-transcripts.py`)
- **Destructive/rename tools:** require dry-run or backup note

## Code Quality Recommendations

### 1. Add an Immich schema preflight

Before `process`, `sync-offline`, `promote` derivative push, and `cluster --apply`,
verify the columns and constraints this repo writes:

- `asset`, `asset_exif`, `asset_file`
- `smart_search.embedding` dimension
- `asset_face`, `face_search`
- `library.importPaths`

This can be a single command:

```sh
immy doctor
```

and a shared internal helper that write paths call automatically.

### 2. Centralize SQL ownership

SQL exists in both `process.py` and `offline.py`, with comments saying it moved
behind `Sink`. Keep one owner for each write surface to reduce schema drift:

- `offline.PgSink` owns online/offline replay SQL
- `promote.py` owns derivative `asset_file` push and album-specific DB queries
- `process.py` stays orchestration-only

### 3. Make lazy module proxies test-friendly

The lazy import pattern is worth keeping because it protects `immy audit` from
ML import cost. The proxy should still behave enough like a module that common
test tooling works:

```python
def __setattr__(self, attr, value):
    setattr(self._load(), attr, value)
```

Also consider loading `pg_mod` eagerly. It is lightweight and heavily patched
by CLI tests.

### 4. Revisit album sync contract

The DB-based album sync is more reliable than filename search, but it changes
the promote surface:

- old behavior: Immich creds were enough
- current behavior: PG is also needed for album assets

Pick one explicit contract:
- **Strict:** album sync requires `pg`; CLI prints a clear skipped reason.
- **Hybrid:** use DB when available, fall back to HTTP lookup when PG is absent.

For this personal deployment, strict is acceptable. The docs and tests should
say so.

### 5. Keep current interruption semantics, but broaden them

Promote now catches `KeyboardInterrupt` and exits 130. Apply the same style to:
- `process` batch runs
- `sync-offline`
- derivative rsync
- long bloat transcodes

The rule: cancellation should stop at the current durable boundary and should
never trigger downstream effects.

## Product Ideas

### 1. Local Immich triage mode

This is the highest-value near-term idea from `raw/TODO.md`.

Deliverables:
- `tools/immich-local/docker-compose.yml`
- `tools/immich-local/link-derivatives.sh`
- `~/.immy/config.local.yml` example
- per-target offline sync
- `immy sync-offline --target local`

Outcome: browse and prune cached trips locally before spending time and network
promoting to vv-nas.

### 2. `immy doctor`

One command to answer:
- config loaded from which path
- Immich API reachable
- PG reachable
- configured library exists and has import paths
- derivative host/container roots look sane
- CLIP dimension matches model
- required local binaries exist: `exiftool`, `ffmpeg`, `ffprobe`, `vips`

This would make first-run and post-upgrade debugging much faster.

### 3. `immy status <trip>`

Useful for batch operations and for returning to a trip weeks later:
- audit HIGH/MEDIUM pending count
- process marker present
- journal phase counts
- offline entries pending/synced by target
- derivative files present/missing
- last heartbeat, if any

### 4. Cluster pruning

Current known residual: `immy cluster` only adds assets. If metadata changes,
assets can remain in old cluster albums.

Recommended design:
- store `asset_id -> cluster_key` in journal or a small immy-owned DB table
- on rerun, remove stale memberships from albums with the `immy-cluster:<key>`
  marker

### 5. Apple Photos people apply path

The dry-run path is shipped. The apply path should wait for a fresh snapshot and
validated match rate, then:
- create people via Immich API
- attach only high-confidence bbox matches
- write an audit report before mutating rows

### 6. CLIP near-duplicate search

Defer until exact duplicate reports have been used on real disks. When built,
keep it separate from `find-duplicates` because it is slower, probabilistic,
and model-dependent.

## Operational Recommendations

### 1. Add a release/checklist habit before Immich upgrades

Before upgrading Immich:
1. Run `immy snapshot`.
2. Run `immy doctor` once it exists.
3. Process a tiny fixture trip into a staging library.
4. Verify thumbs, preview, encoded video, CLIP search, faces, captions, and
   album sync.
5. Only then process/promote real trips.

### 2. Keep `.audit/` as durable working state

The design already treats `.audit/` as durable state. Make that explicit:
- do not clean `.audit/` during normal housekeeping
- backup `.audit/` with trip folders
- if `.audit/` is intentionally deleted, expect reprocessing or reconciliation

### 3. Prefer explicit dry-run flags on maintenance scripts

Some tools already do this well. Any script that renames, deletes, rewrites
front matter, or updates DB rows should default to dry-run and require `--apply`.

## Suggested Next Work Order

1. Land per-target offline sync.
2. Build the local Immich compose + derivative symlink tool.
3. Add `immy doctor`.
4. Add `immy status <trip>`.
5. Revisit cluster pruning and Apple people apply after the local triage loop is
   working.

## Commit Plan

Suggested split for this cleanup:

1. `promote: handle interrupted rsync cleanly`
   - `immy/src/immy/promote.py`
   - `immy/src/immy/cli.py` interrupt-handling hunk
   - `immy/tests/test_promote.py` interrupt test hunk
2. `tests: align with current ingest contracts`
   - `immy/src/immy/cli.py` lazy-module setattr/delattr hunk
   - `immy/tests/test_clip.py`
   - `immy/tests/test_derivatives.py`
   - `immy/tests/test_promote.py`
   - `immy/tests/test_transcripts.py`
3. `docs: sync shipped state and add roadmap`
   - `README.md`
   - `docs/PLAN.md`
   - `docs/TESTING.md`
   - `docs/SIDECAR.md`
   - `docs/ROADMAP.md`
   - `docs/REVIEW-RECOMMENDATIONS.md`
