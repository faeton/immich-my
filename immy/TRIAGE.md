# Footage triage — `immy triage`

Phase 1 tooling for shrinking the trip-video tier (~2.1T as of 2026-07):
gather per-clip signals, rank trips by recoverable bytes, and prepare the
ground for a human review pass. **Nothing in `immy triage` moves, rewrites,
or re-encodes a file** — verdicts are data (`triage` table), and applying
them is a separate, future executor with its own dry-run and quarantine.

## Model

Two manifest tables (schema v3, `dedup/manifest.py`):

- `video_signal` — scan-derived, always safe to rebuild: ffprobe facts
  (duration/codec/bitrate), 6 sampled frames per clip (`frames_json`,
  relative to the frames root), Immich `favorite`/`album_count`, a
  `take_group` id (clips shot in one burst), and an advisory `suggested`
  (`keep` | `compress` | `review-take`) with its reason.
- `triage` — real verdicts only (`keep`/`compress`/`cold`/`trash`), written
  by a human in the review UI (or, later, an explicitly-invoked rule);
  `applied_at` stays NULL until the executor actually acts on one.

The pooled per-clip CLIP vector (mean of the 6 frame embeddings,
L2-normalized) is cached in the existing `embedding` table under the
configured model — same never-embed-twice rule as dedup Stage C.

Take-grouping: within a trip, capture-order clips stay in one take until
the gap exceeds 120 s or their vector's cosine to the running group
centroid drops below 0.80. Missing vectors degrade to time-only grouping.

## Running on n5

The scan needs ffmpeg/ffprobe, the Immich ML endpoint, and the Immich
Postgres — all reachable only inside the deploy/n5 container:

```sh
cd /mnt/flash/immy/immich-my/immy
sudo docker compose -f deploy/n5/compose.yaml build   # pick up new code
sudo docker compose -f deploy/n5/compose.yaml run --rm immy \
  triage scan --manifest /state/manifest.sqlite --config /config/config.yml
sudo docker compose -f deploy/n5/compose.yaml run --rm immy \
  triage report --manifest /state/manifest.sqlite
```

Resumable: ^C any time and re-run — scanned clips are skipped (`--force`
re-scans), frames are reused from `/scratch/triage-frames/<asset_id>/`,
and vectors come from the embedding cache. `--limit N` scans in batches.
The favorite/album lookup is best-effort; `--skip-immich` for offline runs.

`triage report` rolls up per trip: clip count, GB, GB sitting in ≥3-clip
take groups, GB suggested `compress` (>120 s at >60 Mbps, not favorited),
and favorites. `--json` for machines.

## Suggestion rules (advisory, conservative)

1. Immich favorite or album member → `keep` — a human already voted.
2. >120 s and >60 Mbps and not favorited → `compress` candidate.
3. Member of a ≥3-clip take group → `review-take` (pick the best, grade
   the rest by hand).
4. Otherwise no suggestion.
