#!/usr/bin/env bash
# Batch-promote every trip under ~/Media/Trips: rsync originals + derivatives
# to the NAS, drain any offline caches, stack Insta360 pairs. Designed for
# a "good overnight connection" one-shot run — non-interactive, resumable,
# preflighted.
#
# Every step is idempotent:
#   - rsync uses --partial --append --inplace (see promote.py), so a dropped
#     connection mid-file picks up where it left off on rerun.
#   - Trips already fully promoted log a "promoted" event in .audit/audit.jsonl;
#     we skip them unless --force is passed.
#   - The post-rsync API phase (register + stack + derivative rsync) upserts,
#     so a partial API phase reconciles on rerun.
#
# Usage:
#   tools/promote-all-trips.sh            # promote every trip not yet logged
#   tools/promote-all-trips.sh <trip>     # one trip (folder name only)
#   tools/promote-all-trips.sh --status   # report-only; don't promote
#   tools/promote-all-trips.sh --dry-run  # rsync --dry-run for every trip
#   tools/promote-all-trips.sh --force    # redo trips already logged as promoted
#
# Env overrides: TRIPS_ROOT, IMMY_ROOT, LOG_DIR.

set -euo pipefail

TRIPS_ROOT="${TRIPS_ROOT:-$HOME/Media/Trips}"
IMMY_ROOT="${IMMY_ROOT:-$HOME/Sites/immich-my/immy}"
LOG_DIR="${LOG_DIR:-$HOME/.immy/promote-logs}"
CONFIG="${IMMY_CONFIG:-$HOME/.immy/config.yml}"

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
DRY_RUN=0
FORCE=0
TRIP_ARG=""
for arg in "$@"; do
  case "$arg" in
    --status)  STATUS_ONLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --force)   FORCE=1 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    -*) printf 'Unknown flag: %s\n' "$arg" >&2; exit 2 ;;
    *)  TRIP_ARG="$arg" ;;
  esac
done

# --- Preflight -------------------------------------------------------
banner
printf '%sBatch promote run%s  %s\n' "$C_OK" "$C_RESET" \
  "$([[ $STATUS_ONLY -eq 1 ]] && echo '(status only)' \
     || ([[ $DRY_RUN -eq 1 ]] && echo '(dry-run)') )"
banner

[[ -x "$IMMY_BIN" ]] || {
  printf '%sERROR%s immy venv not found at %s — run `uv sync` first.\n' \
    "$C_ERR" "$C_RESET" "$IMMY_BIN"; exit 1
}
[[ -d "$TRIPS_ROOT" ]] || {
  printf '%sERROR%s trips root not found: %s\n' "$C_ERR" "$C_RESET" "$TRIPS_ROOT"; exit 1
}
[[ -f "$CONFIG" ]] || {
  printf '%sERROR%s immy config not found: %s\n' "$C_ERR" "$C_RESET" "$CONFIG"; exit 1
}

# Pull destination out of config for reachability check.
ORIG_ROOT="$(awk '/^originals_root:/ {print $2; exit}' "$CONFIG")"
HOST_ROOT="$(awk '/^[[:space:]]*host_root:/ {print $2; exit}' "$CONFIG")"
printf '  destination (originals): %s\n' "$ORIG_ROOT"
printf '  destination (host_root): %s\n' "$HOST_ROOT"

if [[ $STATUS_ONLY -eq 0 ]]; then
  # If originals_root is user@host:/path, ssh to verify we can reach it
  # and the remote path exists. Saves discovering a dead link 10 hours in.
  if [[ "$ORIG_ROOT" == *:* ]]; then
    SSH_HOST="${ORIG_ROOT%%:*}"
    REMOTE_PATH="${ORIG_ROOT#*:}"
    printf 'Reachability check: ssh %s ...\n' "$SSH_HOST"
    if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$SSH_HOST" \
           "test -d '$REMOTE_PATH'" 2>/dev/null; then
      printf '%sERROR%s ssh %s reachable-or-path-check failed (key auth + %s must exist).\n' \
        "$C_ERR" "$C_RESET" "$SSH_HOST" "$REMOTE_PATH"
      exit 1
    fi
    printf '  %sOK%s  %s:%s reachable\n' "$C_OK" "$C_RESET" "$SSH_HOST" "$REMOTE_PATH"
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

# --- Promoted? -------------------------------------------------------
# A trip is considered promoted when .audit/audit.jsonl contains at least
# one {"event":"promoted"} record. That's what promote.py writes after a
# successful rsync — see log_event(... "event":"promoted") in promote.py.
is_promoted() {
  local audit="$1/.audit/audit.jsonl"
  [[ -f "$audit" ]] && grep -q '"event": *"promoted"' "$audit"
}

# --- Size + status table --------------------------------------------
print_status_table() {
  printf '\n%-42s %10s  %s\n' "trip" "size" "status"
  printf '%-42s %10s  %s\n' "----" "----" "------"
  local total_pending=0
  for trip in "${TRIPS[@]}"; do
    local name size status
    name="$(basename "$trip")"
    size="$(du -sh "$trip" 2>/dev/null | cut -f1)"
    if is_promoted "$trip"; then
      status="${C_DIM}already promoted${C_RESET}"
    else
      status="${C_WARN}pending${C_RESET}"
      total_pending=$((total_pending + 1))
    fi
    printf '%-42s %10s  %b\n' "$name" "$size" "$status"
  done
  local total_size
  total_size="$(du -shc "${TRIPS[@]}" 2>/dev/null | tail -1 | cut -f1)"
  printf '\n%d trip(s), %s total, %d pending\n' \
    "${#TRIPS[@]}" "$total_size" "$total_pending"
}

printf 'Found %d trip(s) under %s.\n' "${#TRIPS[@]}" "$TRIPS_ROOT"
print_status_table

if [[ $STATUS_ONLY -eq 1 ]]; then
  exit 0
fi

# --- Run -------------------------------------------------------------
INTERRUPTED=0
trap 'INTERRUPTED=1' INT

printf '\nLogging to %s\n' "$RUN_LOG"
PROMOTE_FLAGS=()
[[ $DRY_RUN -eq 1 ]] && PROMOTE_FLAGS+=(--dry-run)

TOTAL_OK=0; TOTAL_FAIL=0; TOTAL_SKIP=0

# caffeinate -dims: keep Mac awake (no display, no idle, no system sleep)
# for the full overnight run. Without this, a closed-lid or idle-sleep
# mid-transfer kills rsync. nice -n 10 keeps interactive apps responsive
# if you check in on the laptop.
for trip in "${TRIPS[@]}"; do
  name="$(basename "$trip")"
  if [[ $FORCE -eq 0 ]] && is_promoted "$trip"; then
    printf '%s[skip]%s %s (already promoted)\n' \
      "$C_DIM" "$C_RESET" "$name" | tee -a "$RUN_LOG"
    TOTAL_SKIP=$((TOTAL_SKIP + 1))
    continue
  fi

  banner | tee -a "$RUN_LOG"
  printf '%s[%s]%s %s\n' "$C_OK" "$(date +%H:%M:%S)" "$C_RESET" "$name" \
    | tee -a "$RUN_LOG"
  banner | tee -a "$RUN_LOG"

  # Apply HIGH findings (date/GPS-from-SRT, trip-tags, timezone, etc.)
  # before promote — promote.py gates on pending HIGH findings. --auto
  # skips the interactive trip-anchor prompt for trips lacking GPS.
  if [[ $DRY_RUN -eq 0 ]]; then
    if ! caffeinate -dims nice -n 10 \
           "$IMMY_BIN" audit "$trip" --write --auto \
           2>&1 | tee -a "$RUN_LOG"; then
      rc=${PIPESTATUS[0]}
      TOTAL_FAIL=$((TOTAL_FAIL + 1))
      printf '%s[fail rc=%s audit]%s %s\n' \
        "$C_ERR" "$rc" "$C_RESET" "$name" | tee -a "$RUN_LOG"
      continue
    fi
  fi

  if caffeinate -dims nice -n 10 \
       "$IMMY_BIN" promote "$trip" "${PROMOTE_FLAGS[@]}" \
       2>&1 | tee -a "$RUN_LOG"; then
    TOTAL_OK=$((TOTAL_OK + 1))
  else
    rc=${PIPESTATUS[0]}
    TOTAL_FAIL=$((TOTAL_FAIL + 1))
    printf '%s[fail rc=%s]%s %s\n' \
      "$C_ERR" "$rc" "$C_RESET" "$name" | tee -a "$RUN_LOG"
  fi

  if (( INTERRUPTED )); then
    printf '%sInterrupted%s — stopping batch.\n' \
      "$C_WARN" "$C_RESET" | tee -a "$RUN_LOG"
    break
  fi
done

# --- Summary ---------------------------------------------------------
banner | tee -a "$RUN_LOG"
printf 'Done. %s%d ok%s, %s%d failed%s, %s%d skipped%s. Log: %s\n' \
  "$C_OK" "$TOTAL_OK" "$C_RESET" \
  "$C_ERR" "$TOTAL_FAIL" "$C_RESET" \
  "$C_DIM" "$TOTAL_SKIP" "$C_RESET" \
  "$RUN_LOG" | tee -a "$RUN_LOG"
banner | tee -a "$RUN_LOG"
printf '\nFinal status:\n' | tee -a "$RUN_LOG"
print_status_table | tee -a "$RUN_LOG"
