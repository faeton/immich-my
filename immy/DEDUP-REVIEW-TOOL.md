# Dedup review tool — `immy dedup review-server`

Built 2026-07-12 per the dedup-review build brief. A single-user web UI that
walks the human through every `decision='review'` cluster (safest-first:
highest `clip_cos_sim`) and records the outcome in `manifest.sqlite` via
`engine.commit_cluster_decision()` — the same write path `decide()` uses, so
`dedup apply` (unmodified) picks up the results exactly like machine-made
decisions. The tool never moves, copies, or deletes a media file.

> Lives in the deployed copy at `/mnt/flash/immy/src-immy` on n5 — sync this
> file and the code changes back to the main repo (`raw/DEDUP-REVIEW-TOOL.md`).

## Run it (on n5)

Tailnet-exposed (chosen mode 2026-07-12 — reachable from any tailscale
device, still invisible to the LAN; the publish binds only the tailscale0
interface, `100.115.236.50`):

```sh
sudo docker compose -f /mnt/flash/immy/src-immy/deploy/n5/compose.yaml run --rm \
  -d --name immy-dedup-review \
  --publish 100.115.236.50:8765:8765 \
  immy dedup review-server --manifest /state/manifest.sqlite
```

Open http://100.115.236.50:8765 (or http://n5:8765 with MagicDNS). Stop with
`sudo docker stop immy-dedup-review`. For the stricter localhost+ssh-tunnel
mode, publish `127.0.0.1:8765:8765` instead and use
`ssh -L 8765:localhost:8765 n5`.

Note the app binds `0.0.0.0` **inside** the container — a docker `--publish`
forwards to the container's eth0, not its loopback, so an in-container
127.0.0.1 bind would be unreachable. The interface prefix on the publish
spec is what limits exposure.

## Actions / keys

| action | key | manifest write |
|---|---|---|
| Duplicates — keep selected | `Enter` (or `1`–`9` to pick, then Enter) | `decision='auto'`, winner + roles, members `clustered→decided` |
| Not duplicates — keep all | `K` | `decision='kept_all'`, statuses untouched |
| Skip (decide later) | `S` / `→` | nothing (in-memory only; resets on restart) |
| Zoom selected | `Z` / space / alt-click | — |

The `winner_score()`-best member is pre-selected. If a cluster contains a
`library/originals` member, the winner is **locked** to the originals copy
(server-enforced too): letting a staging file win would make `apply` promote
a second copy next to the canonical one — a swap is a manual job.

## Scope

v1 covers the image clusters that have a `clip_cos_sim` (Stage C ran).
Video/no-CLIP review clusters (~1.9k) are counted in the footer but not
served — v2 needs ffmpeg poster frames and a different sort key.

## Internals

- `src/immy/dedup/review.py` — Flask app (server-rendered HTML, no build
  step). One sqlite connection per request; one commit per decision (short
  WAL lock windows, safe alongside a running batch job — they touch disjoint
  decision states).
- `engine.commit_cluster_decision()` — extracted from `decide()` so the
  machine and human write paths stay identical by construction.
- Speed: a daemon thumb-warmer thread pre-generates 640px thumbnails in
  queue order ahead of the reviewer, and each page prefetches the next two
  clusters' thumbnails into the browser cache — decisions paint the next
  cluster instantly. The `winner_score()`-recommended keeper renders
  pre-selected, so the common gesture is a single Enter per cluster.
- Thumbnails: lazy fallback, cached under `/scratch/dedup-review-tool/{640,1600}/<asset_id>.jpg`
  (`gen_triage.py`'s direct-pyvips-then-exiftool-preview strategy, tmp+rename
  writes). Never regenerated once present.
- Tests: `tests/test_dedup.py` (commit_cluster_decision status contract),
  `tests/test_dedup_review.py` (routes, 409 on stale tab, originals guard).
  Run in-container:
  `sudo docker compose -f deploy/n5/compose.yaml run --rm --entrypoint bash \
   -v /mnt/flash/immy/src-immy:/repo immy -c "pip install -q pytest pytest-xdist && cd /repo && python -m pytest tests/test_dedup.py tests/test_dedup_review.py"`
- New deps: `flask>=3.0` (pyproject + Dockerfile.immy — image rebuilt 2026-07-12).

## Verified 2026-07-12

Against a snapshot copy (`/state/manifest.sqlite.review-tool-test`, taken
with sqlite's backup API while a live `apply --write` ran): queue order,
640/1600 thumbnails (JPEG + RAW fallback), a real merge (wrote
`auto`/winner/roles/`decided` identically to `decide()`), double-submit →
409, originals lock page, skip flow. 48/48 tests pass. The snapshot copy can
be deleted whenever.
