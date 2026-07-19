# Footage triage — `immy triage`

Tooling for shrinking the trip-video tier (~2.1T as of 2026-07): gather
per-clip signals, rank trips by recoverable bytes (phase 1: `scan`,
`report`), and grade every clip by hand in a web UI (phase 2:
`review-server`). **Nothing in `immy triage` moves, rewrites, or re-encodes
a file** — verdicts are data (`triage` table), and applying them is a
separate, future executor with its own dry-run and quarantine.

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
take groups, GB suggested `compress` (>120 s at >40 Mbps, not favorited),
and favorites. `--json` for machines.

## Review UI — `immy triage review-server`

The human pass. One trip per screen (index sorted by undecided GB — review
top-down for the biggest payoff), clips in capture order grouped into take
blocks, each clip a contact sheet of the scan's 6 cached frames plus
duration/size/bitrate/codec and the advisory suggestion. Keyboard-first:

    K keep · C compress · A archive (cold) · T trash · U undo
    shift+key = whole take · ↓/J/↑ move · Z zoom · P play · H hide decided

The zoom lightbox cycles frames (X/←→) and plays mp4/mov in-browser via
Range streaming (HEVC needs Safari or hw-decode Chrome); .insv/.360 can't
play — grade those from the frames. Verdict keys work inside the zoom.

Verdicts are upserts into `triage` (`decided_by='human'`, `decided_at`
UTC); U deletes the row. A verdict the executor has already applied
(`applied_at` set) renders with a dashed outline and is locked — the UI
refuses to change it (409), because a changed verdict would silently
disagree with what's on disk.

On n5 (frames + manifest live in the standard container mounts):

```sh
sudo docker compose -f deploy/n5/compose.yaml run --rm \
  --name immy-triage-review --publish 100.115.236.50:8766:8766 \
  immy triage review-server --manifest /state/manifest.sqlite
```

then open `http://n5.bee-ruffe.ts.net:8766` from anywhere on the tailnet
(port 8765 stays with the dedup review server).

## Suggestion rules (advisory, conservative)

1. Immich favorite → `keep` — a human already voted. Album membership is
   deliberately **not** a signal: immy's auto-albums cover every trip clip,
   and on the first n5 scan the album rule blanket-kept 1.86 TB.
2. >120 s and >40 Mbps and not favorited → `compress` candidate (n5's
   H.264 averages 51 Mbps, HEVC 88 Mbps — the old 60 Mbps bar excluded
   most of the re-encodable long tail).
3. Member of a ≥3-clip take group → `review-take` (pick the best, grade
   the rest by hand).
4. Otherwise no suggestion.

Rules are recomputed on every `scan` (derived layer, no `--force` needed),
so retuning them is a code edit + a cheap re-scan.
