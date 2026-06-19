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
# CLIP + derivatives are on by default. Faces are forced OFF: the lean NAS
# image omits onnxruntime/insightface, so face detection runs on the Mac, not
# here. (Put --with-faces in your extra flags only if you've added those deps.)
set -eu

COMPOSE="${IMMY_COMPOSE:-/mnt/flash/immy/compose.yaml}"

exec sudo docker compose -f "$COMPOSE" run --rm immy \
  process --no-faces "$@"
