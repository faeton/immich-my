#!/bin/zsh
# Group B — process-only (NO promote) for the ~25 EXISTING trips that immy
# match found to have un-ingested new laptop files (svalbard 102, vietnam 93,
# scotland 46, the norways, cyprus×N, …). process is incremental + idempotent:
# the already-ingested files are cached, only the new ones get worked. Promote
# is deferred (network) like the main batch. Smallest folder first (fail-fast),
# svalbard (2495 files) last.
#
# Chained to run AFTER process_all_local.sh so MLX/DB aren't contended — see
# the waiter that launches this. Standalone run is fine too.
set -u
exec < /dev/null
export IMMY_CONFIG=/Users/faeton/Sites/immich-my/.immy/config.yml
cd /Users/faeton/Sites/immich-my/immy
TR=/Users/faeton/Media/Trips

TRIPS=(
  2025-06-italy-naples 2024-05-przemysl 2025-08-london 2024-04-italy-churva
  2025-02-cyprus-wreck-velchev 2025-02-cyprus-wreck 2025-01-beach-amg-gt-flight
  2023-08-serbia 2024-05-austria 2023-08-croatia 2025-07-austria-hotel
  2024-06-cyprus-leechan 2024-04-jurmala 2026-02-mau-whales 2025-08-iceland-volcano
  2025-08-la-manga 2025-06-norway 2024-12-cyprus 2024-07-norway 2025-05-ryzh-paphos-fpv
  2025-07-scotland 2025-01-norway 2025-02-vietnam 2025-11-pacific-tonga
  2025-06-svalbard-arctic
)

for t in $TRIPS; do
  echo "################  $t  $(date '+%H:%M:%S')  ################"
  uv run immy audit --write --auto "$TR/$t" || { echo "AUDIT FAIL $t"; continue; }
  uv run immy process --no-clip --no-faces --with-captions --with-transcripts --force "$TR/$t" || { echo "PROCESS FAIL $t"; continue; }
  echo "DONE $t  $(date '+%H:%M:%S')"
done
echo "################  GROUP B ALL DONE  $(date '+%F %H:%M:%S')  ################"
