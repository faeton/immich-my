#!/bin/zsh
# Overnight: audit -> process -> promote the 12 new trips + the antarctica
# drift into n5's live Immich. ~152 GB of originals, ~11-17 h at 20-30 Mbps.
#
# Safe to re-run: immy is idempotent (checksum-keyed journal + rsync), so a
# killed run resumes where it stopped. Each trip is independent; a failure
# logs and moves on.
#
#   --no-clip/--no-faces : let Immich's own ML backfill embeddings + faces
#       (avoids MLX-vs-immich-ml model mismatch; immy still makes the
#       GO2-dewarped thumbnails, which is the whole point).
#   --force on promote   : these trips have no EXIF GPS (GO2/Insta360 strip
#       it). Get the media in now; geotag later — drone via `immy srt`
#       (rollout #8), GO2 by hand. Drop --force per-trip once you've set a
#       location if you'd rather gate.
#
# Run:  nohup zsh immy/scripts/overnight_promote.sh > ~/promote-$(date +%F).log 2>&1 &
set -u
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
  2026-04-socotra                      # biggest last (41 GB)
  2024-03-antarctica                   # drift: 2 GO2 files merged in
)

for t in $TRIPS; do
  echo "################  $t  $(date '+%H:%M:%S')  ################"
  uv run immy audit --write   "$TR/$t" || { echo "AUDIT FAIL $t"; continue; }
  uv run immy process --no-clip --no-faces "$TR/$t" || { echo "PROCESS FAIL $t"; continue; }
  uv run immy promote --force "$TR/$t" || { echo "PROMOTE FAIL $t"; continue; }
  echo "DONE $t  $(date '+%H:%M:%S')"
done
echo "################  ALL DONE  $(date '+%F %H:%M:%S')  ################"
