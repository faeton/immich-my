#!/bin/zsh
# Process-only (NO promote): run the heavy local MLX work for all 15 pending
# trips — derivatives, GO2-dewarped thumbs, captions, transcripts — and write
# the .audit/ artifacts next to each trip. Upload/promote/sync is deferred:
# do it later from a good uplink with `immy promote` (or overnight_promote.sh).
#
# Why split: process is CPU/GPU-bound (Mac MLX), promote is network-bound
# (mobile 5-30 Mbps, often off-tailnet). Compute now, ship later.
#
#   --no-clip/--no-faces : do NOT compute CLIP/faces locally — MLX embeddings
#       live in a different vector space than n5's immich-ml and would break
#       smart search (clip.py: "NOT interchangeable"). They get produced by n5's
#       own immich-ml AFTER promote. NB: because immy pre-inserts asset rows,
#       Immich's library scan won't auto-queue SmartSearch/FaceDetection — that
#       pass must be triggered explicitly (clip_backend: immich-ml on n5, or the
#       Immich admin "queue missing" jobs) or smart search stays silently empty.
#   --with-captions --with-transcripts : run the LOCAL enrichment now — gemma
#       VLM captions (LM Studio, pinned `gemma-4-31b-it`) + mlx-whisper ASR.
#       Needs the `ml:` block in .immy/config.yml.
#   --force : bypass the trip-level `is_trip_fully_cached` marker (the earlier
#       derivatives-only pass wrote markers; without --force the whole trip is
#       skipped before the caption/transcript phases run). Per-ASSET journal
#       gating still holds, so cached derivatives are NOT re-transcoded.
#
# Safe to re-run: immy is idempotent (checksum-keyed journal). A killed run
# resumes where it stopped; each trip is independent (a failure logs + moves on).
#
# Run:  nohup zsh immy/scripts/process_all_local.sh > ~/process-$(date +%F).log 2>&1 &
set -u
# Detach stdin: this runs unattended in the background. Without it, any tool
# that touches the tty gets SIGTTIN (job suspends), and a suspended+resumed
# Python crashes at startup with "Bad file descriptor" initializing streams.
exec < /dev/null
export IMMY_CONFIG=/Users/faeton/Sites/immich-my/.immy/config.yml
cd /Users/faeton/Sites/immich-my/immy
TR=/Users/faeton/Media/Trips

TRIPS=(
  2022-11-krakow                       # smallest first — fails fast if anything's wrong
  2024-01-airport-timelapse
  2025-02-cyprus-purish
  2023-07-iceland-with-finayev
  2024-12-ivan-photoshoot
  2023-11-egypt
  2022-09-london-fire-trucking
  2022-12-warsaw-fedorenko-tatevosyan
  2022-10-oxford-tesla-battery
  2023-01-cyprus-masha-interview-backstage
  2023-06-tesla-travel-lapses-dotaplay
  2026-04-socotra                      # 41 GB
  2024-03-antarctica                   # drift: 2 GO2 files merged in
  2024-03-usa-florida                  # 86 videos — un-ingested drone trip
  2024-04-namibia                      # 324 videos — heaviest; last
)

for t in $TRIPS; do
  echo "################  $t  $(date '+%H:%M:%S')  ################"
  uv run immy audit --write --auto "$TR/$t" || { echo "AUDIT FAIL $t"; continue; }
  uv run immy process --no-clip --no-faces --with-captions --with-transcripts --force "$TR/$t" || { echo "PROCESS FAIL $t"; continue; }
  echo "DONE $t  $(date '+%H:%M:%S')"
done
echo "################  ALL DONE  $(date '+%F %H:%M:%S')  ################"
