#!/usr/bin/env bash
# Batch-process every trip folder under ~/Media/Trips using Gemma 4 via
# LM Studio on localhost:1234. Runs the FULL pipeline (derivatives +
# CLIP + faces + transcripts + captions) because every phase is
# individually idempotent — re-runs are cheap, and this way a half-
# ingested trip finishes regardless of which pass stopped it.
#
# Usage:
#   tools/caption-all-trips.sh              # process every trip (offline by default)
#   tools/caption-all-trips.sh <trip-name>  # one trip (folder name only)
#   tools/caption-all-trips.sh --status     # report-only; don't process
#   tools/caption-all-trips.sh --online     # write straight to DB (needs tailnet)
#   tools/caption-all-trips.sh --sync       # replay any cached .audit/offline/ to DB
#
# Offline-by-default: every phase (CLIP, faces, transcripts, captions)
# runs locally and writes to `.audit/offline/<checksum>.yml` per trip.
# Later, when the tailnet is up, re-run with `--sync` (or call
# `immy sync-offline <trip>` per folder) to push the tiny SQL traffic.
# This means overnight runs work on a plane/cafe without NAS connectivity.
#
# Prerequisites (see docs/OFFLINE-RUNBOOK.md):
#   - LM Studio running, Gemma 4 loaded, server started on :1234
#   - `uv sync` has been run in immy/
#   - ~/.immy/config.yml has media: + ml.captioner: configured
#   - ~/.immy/library.yml cached (auto-written after any online `immy process`)
#
# Env overrides: TRIPS_ROOT, IMMY_ROOT, LMSTUDIO_URL, MODEL, LOG_DIR.

set -euo pipefail

TRIPS_ROOT="${TRIPS_ROOT:-$HOME/Media/Trips}"
IMMY_ROOT="${IMMY_ROOT:-$HOME/Sites/immich-my/immy}"
LMSTUDIO_URL="${LMSTUDIO_URL:-http://localhost:1234/v1}"
MODEL="${MODEL:-google/gemma-4-26b-a4b}"
# Logs live outside the media tree so immy's rglob-based file scan can
# never touch them. `.audit` inside each trip folder is the only media-
# tree state immy writes; everything else about this script lives here.
LOG_DIR="${LOG_DIR:-$HOME/.immy/captions-logs}"

IMMY_BIN="$IMMY_ROOT/.venv/bin/immy"

if [[ -t 1 ]]; then
  C_OK=$'\033[0;32m'; C_WARN=$'\033[0;33m'; C_ERR=$'\033[0;31m'
  C_DIM=$'\033[2m'; C_RESET=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_RESET=""
fi

banner() { printf '%s%s%s\n' "$C_DIM" "--------------------------------------------------" "$C_RESET"; }

# --- Args ------------------------------------------------------------
STATUS_ONLY=0
SYNC_ONLY=0
MODE="offline"  # offline | online
TRIP_ARG=""
for arg in "$@"; do
  case "$arg" in
    --status) STATUS_ONLY=1 ;;
    --sync)   SYNC_ONLY=1 ;;
    --online) MODE="online" ;;
    --offline) MODE="offline" ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    -*) printf 'Unknown flag: %s\n' "$arg" >&2; exit 2 ;;
    *)  TRIP_ARG="$arg" ;;
  esac
done

# --- Preflight -------------------------------------------------------
banner
printf '%sBatch caption run%s  %s\n' "$C_OK" "$C_RESET" \
  "$([[ $STATUS_ONLY -eq 1 ]] && echo '(status only)' || echo '')"
banner

[[ -x "$IMMY_BIN" ]] || {
  printf '%sERROR%s immy venv not found at %s — run `uv sync` first.\n' \
    "$C_ERR" "$C_RESET" "$IMMY_BIN"; exit 1
}
[[ -d "$TRIPS_ROOT" ]] || {
  printf '%sERROR%s trips root not found: %s\n' "$C_ERR" "$C_RESET" "$TRIPS_ROOT"; exit 1
}

if [[ $STATUS_ONLY -eq 0 && $SYNC_ONLY -eq 0 ]]; then
  # Offline-readiness preflight. Every phase except EXIF needs a cached
  # model; if any are missing we fail now rather than 40 photos into a
  # run with no internet to recover. Paths below match what each module
  # actually looks at — verified against faces.py, clip.py, transcripts.py.
  MISSING=0
  check_path() {
    local label="$1" path="$2"
    if [[ -e "$path" ]]; then
      printf '  %sOK%s  %-18s %s\n' "$C_OK" "$C_RESET" "$label" "$path"
    else
      printf '  %sMISS%s %-18s %s\n' "$C_ERR" "$C_RESET" "$label" "$path"
      MISSING=$((MISSING + 1))
    fi
  }

  printf 'Offline model cache check:\n'
  check_path "CLIP (mlx-clip)"    "$IMMY_ROOT/mlx-community/clip-vit-base-patch32"
  check_path "Whisper large-v3"   "$HOME/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-mlx"
  check_path "InsightFace buffalo_l" "$HOME/.insightface/models/buffalo_l"
  if [[ "$MODE" == "offline" ]]; then
    # Library cache is a nice-to-have but not required: `immy process
    # --offline` falls back to recovering container_root from any
    # existing `.audit/y_processed.yml` marker, and sync-offline
    # later fills in owner/library UUIDs from the live DB. Only WARN.
    if [[ -e "$HOME/.immy/library.yml" ]]; then
      printf '  %sOK%s  %-18s %s\n' "$C_OK" "$C_RESET" "library cache" \
        "$HOME/.immy/library.yml"
    else
      printf '  %sWARN%s %-18s %s (will recover from trip markers)\n' \
        "$C_WARN" "$C_RESET" "library cache" "$HOME/.immy/library.yml"
    fi
  fi

  printf 'Checking LM Studio at %s ...\n' "$LMSTUDIO_URL"
  if ! MODELS_JSON="$(curl -fsS -m 5 "$LMSTUDIO_URL/models" 2>/dev/null)"; then
    printf '  %sMISS%s %-18s server not responding (start it in the Developer tab)\n' \
      "$C_ERR" "$C_RESET" "LM Studio"
    MISSING=$((MISSING + 1))
  elif ! printf '%s' "$MODELS_JSON" | grep -q "\"$MODEL\""; then
    printf '  %sMISS%s %-18s model %s not loaded\n' \
      "$C_ERR" "$C_RESET" "LM Studio" "$MODEL"
    MISSING=$((MISSING + 1))
  else
    printf '  %sOK%s  %-18s %s (serving %s)\n' \
      "$C_OK" "$C_RESET" "LM Studio" "$LMSTUDIO_URL" "$MODEL"
  fi

  if [[ $MISSING -gt 0 ]]; then
    printf '\n%sERROR%s %d model cache(s) missing. Fetch while online, then re-run.\n' \
      "$C_ERR" "$C_RESET" "$MISSING"
    exit 1
  fi
fi

mkdir -p "$LOG_DIR"
RUN_TS="$(date +%Y%m%d-%H%M%S)"
RUN_LOG="$LOG_DIR/run-$RUN_TS.log"

# --- Pick trips ------------------------------------------------------
declare -a TRIPS
if [[ -n "$TRIP_ARG" ]]; then
  TRIPS=("$TRIPS_ROOT/$TRIP_ARG")
  [[ -d "${TRIPS[0]}" ]] || {
    printf '%sERROR%s no such trip: %s\n' "$C_ERR" "$C_RESET" "${TRIPS[0]}"; exit 1
  }
else
  while IFS= read -r -d '' dir; do
    TRIPS+=("$dir")
  done < <(find "$TRIPS_ROOT" -mindepth 1 -maxdepth 1 -type d \
             ! -name '.*' -print0 | sort -z)
fi

# --- Per-trip status reader ------------------------------------------
# Parses `<trip>/.audit/process.yml` (produced by `immy process`) with a
# small Python shim. Returns a tab-separated row: processed_at, inserted,
# already, clip, faces, transcripts, captions. Empty line if no marker.
read_trip_status() {
  local trip="$1"
  "$IMMY_BIN" --help >/dev/null 2>&1 || true  # sanity; not fatal
  "$IMMY_ROOT/.venv/bin/python" - "$trip" <<'PY' 2>/dev/null || true
import sys, yaml
from pathlib import Path
marker = Path(sys.argv[1]) / ".audit" / "process.yml"
if not marker.is_file():
    sys.exit(0)
d = yaml.safe_load(marker.read_text()) or {}
from datetime import datetime, timezone
ts = d.get("processed_at")
when = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else "-"
print("\t".join(str(x) for x in [
    when,
    d.get("inserted", 0),
    d.get("already_present", 0),
    d.get("clip_embedded", 0),
    d.get("faces_detected", 0),
    d.get("transcripts_written", 0),
    d.get("captions_written", 0),
]))
PY
}

print_status_table() {
  printf '\n%-40s %-16s %7s %7s %7s %7s %7s %7s\n' \
    "trip" "last run" "new" "existing" "clip" "faces" "srt" "captions"
  printf '%-40s %-16s %7s %7s %7s %7s %7s %7s\n' \
    "----" "--------" "---" "--------" "----" "-----" "---" "--------"
  for trip in "${TRIPS[@]}"; do
    local name status
    name="$(basename "$trip")"
    status="$(read_trip_status "$trip")"
    if [[ -z "$status" ]]; then
      printf '%-40s %s%-16s%s %7s %7s %7s %7s %7s %7s\n' \
        "$name" "$C_DIM" "(never processed)" "$C_RESET" \
        "-" "-" "-" "-" "-" "-"
    else
      IFS=$'\t' read -r ts ins ex cl fa tr cap <<<"$status"
      printf '%-40s %-16s %7s %7s %7s %7s %7s %7s\n' \
        "$name" "$ts" "$ins" "$ex" "$cl" "$fa" "$tr" "$cap"
    fi
  done
  printf '\n'
}

printf 'Found %d trip(s). Status before run:\n' "${#TRIPS[@]}"
print_status_table

if [[ $STATUS_ONLY -eq 1 ]]; then
  exit 0
fi

# --- Run -------------------------------------------------------------
export IMMY_CAPTIONER_ENDPOINT="$LMSTUDIO_URL"
export IMMY_CAPTIONER_MODEL="$MODEL"

# Thermal/crash mitigations for long overnight runs:
#   - KMP_DUPLICATE_LIB_OK: Homebrew libvips and torch's bundled libomp
#     both end up loaded; without this the second libomp aborts the
#     process at torch import time.
#   - *_NUM_THREADS / VIPS_CONCURRENCY: cap ONNX/BLAS/vips worker pools
#     at half the cores so the Mac doesn't pin every core to 100% for
#     hours. Drop IMMY_THREADS in the env to override (e.g. 2 on hot
#     days, 8 for a fast AC run).
: "${IMMY_THREADS:=4}"
export KMP_DUPLICATE_LIB_OK=TRUE
export OMP_NUM_THREADS="$IMMY_THREADS"
export MKL_NUM_THREADS="$IMMY_THREADS"
export OPENBLAS_NUM_THREADS="$IMMY_THREADS"
export VECLIB_MAXIMUM_THREADS="$IMMY_THREADS"
export VIPS_CONCURRENCY="$IMMY_THREADS"

# Ctrl-C handling: without a trap, SIGINT kills the current `immy` (pipe
# returns nonzero) and the for-loop marches on to the next trip. That's
# the opposite of what you want when you hit ^C. Catch it here and break
# after the current trip's command has unwound.
INTERRUPTED=0
trap 'INTERRUPTED=1' INT

printf 'Logging to %s\n' "$RUN_LOG"
printf 'Mode: %s%s%s\n' "$C_OK" \
  "$([[ $SYNC_ONLY -eq 1 ]] && echo 'sync-offline (push cached work to DB)' \
     || ([[ "$MODE" == "offline" ]] && echo 'offline (cache to .audit/offline/)' \
     || echo 'online (write straight to Postgres)'))" \
  "$C_RESET"

TOTAL_OK=0; TOTAL_FAIL=0
for trip in "${TRIPS[@]}"; do
  name="$(basename "$trip")"
  banner | tee -a "$RUN_LOG"
  printf '%s[%s]%s %s\n' "$C_OK" "$(date +%H:%M:%S)" "$C_RESET" "$name" \
    | tee -a "$RUN_LOG"
  banner | tee -a "$RUN_LOG"

  if [[ $SYNC_ONLY -eq 1 ]]; then
    if "$IMMY_BIN" sync-offline "$trip" 2>&1 | tee -a "$RUN_LOG"; then
      TOTAL_OK=$((TOTAL_OK + 1))
    else
      TOTAL_FAIL=$((TOTAL_FAIL + 1))
    fi
    if (( INTERRUPTED )); then
      printf '%sInterrupted%s — stopping batch.\n' \
        "$C_WARN" "$C_RESET" | tee -a "$RUN_LOG"
      break
    fi
    continue
  fi

  # Full pipeline. Every phase is idempotent individually, so re-runs
  # on an already-processed trip are cheap (checksum match skips ML).
  # Captions are the slow part; caffeinate keeps the Mac awake.
  PROCESS_FLAGS=(--with-derivatives --with-clip --with-faces
                 --with-transcripts --with-captions)
  [[ "$MODE" == "offline" ]] && PROCESS_FLAGS+=(--offline)
  # `nice -n 10` yields to the foreground so the fan controller has
  # headroom on an hours-long run. Pair with the *_NUM_THREADS caps above.
  if caffeinate -dims nice -n 10 "$IMMY_BIN" process "$trip" \
       "${PROCESS_FLAGS[@]}" \
       2>&1 | tee -a "$RUN_LOG"; then
    TOTAL_OK=$((TOTAL_OK + 1))
  else
    rc=$?
    TOTAL_FAIL=$((TOTAL_FAIL + 1))
    printf '%sFAILED%s %s (rc=%s)\n' \
      "$C_ERR" "$C_RESET" "$name" "$rc" | tee -a "$RUN_LOG"
  fi
  if (( INTERRUPTED )); then
    printf '%sInterrupted%s — stopping batch after %s.\n' \
      "$C_WARN" "$C_RESET" "$name" | tee -a "$RUN_LOG"
    break
  fi
done

# --- Summary ---------------------------------------------------------
banner | tee -a "$RUN_LOG"
printf 'Done. %s%d ok%s, %s%d failed%s. Log: %s\n' \
  "$C_OK" "$TOTAL_OK" "$C_RESET" \
  "$C_ERR" "$TOTAL_FAIL" "$C_RESET" \
  "$RUN_LOG" | tee -a "$RUN_LOG"
banner | tee -a "$RUN_LOG"
printf '\nFinal status:\n' | tee -a "$RUN_LOG"
print_status_table | tee -a "$RUN_LOG"
