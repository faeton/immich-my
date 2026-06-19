#!/bin/sh
# immy batch enrichment on the N5 — a one-shot container on Immich's docker
# network. No idle daemon: `docker compose run --rm` starts, runs, exits.
#
# Usage:
#   ./run-batch.sh /originals/<trip> [extra immy process flags]
# Examples:
#   ./run-batch.sh /originals/2026-bali --dry-run          # safe: no DB writes
#   ./run-batch.sh /originals/2026-bali --with-captions --with-transcripts
#
# NAS enrichment scope = CAPTIONS + TRANSCRIPTS. Immich keeps doing its own
# CLIP/faces/thumbnails (its ML is on by default), so we run --no-clip
# --no-faces here. Captions/transcripts that already exist (by DB AI-prefix /
# existing .srt sidecar) are skipped automatically — so this is safe to re-run
# and the Mac and NAS won't redo each other's work.
set -eu

COMPOSE="${IMMY_COMPOSE:-/mnt/flash/immy/src-immy/deploy/n5/compose.yaml}"

exec sudo docker compose -f "$COMPOSE" run --rm immy \
  process --with-captions --with-transcripts --no-clip --no-faces "$@"
