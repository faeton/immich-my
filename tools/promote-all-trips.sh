#!/usr/bin/env bash
# Batch-promote every trip under ~/Media/Trips: rsync originals + derivatives
# to the NAS, drain any offline caches, stack Insta360 pairs. Designed for
# a "good overnight connection" one-shot run — non-interactive, resumable,
# preflighted.
#
# Every step is idempotent:
#   - rsync uses --partial --inplace (see promote.py), plus --append-verify
#     where the local rsync supports it, so a dropped connection mid-file
#     picks up where it left off on rerun without trusting unverified bytes.
#   - Trips already fully promoted log a "promoted" event in .audit/audit.jsonl;
#     we skip them unless --force is passed.
#   - The post-rsync API phase (register + stack + derivative rsync) upserts,
#     so a partial API phase reconciles on rerun.
#
# Usage:
#   tools/promote-all-trips.sh            # promote every trip not yet logged
#   tools/promote-all-trips.sh <trip>     # one trip (folder name only)
#   tools/promote-all-trips.sh '2024-*'   # glob: every trip matching pattern
#   tools/promote-all-trips.sh a b c      # multiple trips / patterns
#   tools/promote-all-trips.sh --status   # report-only; don't promote
#   tools/promote-all-trips.sh --dry-run  # rsync --dry-run for every trip
#   tools/promote-all-trips.sh --force    # redo trips already logged as promoted
#
# While running, press Ctrl+T (SIGINFO) to print live progress: current trip,
# index/total + %, elapsed time on this trip, total run time, ok/fail/skip.
# Status is also mirrored to $LOG_DIR/run-<ts>.status — `cat` it from any
# other terminal to peek without touching the running shell.
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
TRIP_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --status)  STATUS_ONLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --force)   FORCE=1 ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    -*) printf 'Unknown flag: %s\n' "$arg" >&2; exit 2 ;;
    *)  TRIP_ARGS+=("$arg") ;;
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
SSH_HOST=""
REMOTE_PATH=""
if [[ "$ORIG_ROOT" == *:* ]]; then
  SSH_HOST="${ORIG_ROOT%%:*}"
  REMOTE_PATH="${ORIG_ROOT#*:}"
fi
printf '  destination (originals): %s\n' "$ORIG_ROOT"
printf '  destination (host_root): %s\n' "$HOST_ROOT"

if [[ $STATUS_ONLY -eq 0 ]]; then
  # If originals_root is user@host:/path, ssh to verify we can reach it
  # and the remote path exists. Saves discovering a dead link 10 hours in.
  if [[ -n "$SSH_HOST" ]]; then
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
STATUS_FILE="$LOG_DIR/run-$RUN_TS.status"

# --- Pick trips ------------------------------------------------------
declare -a TRIPS
if [[ ${#TRIP_ARGS[@]} -gt 0 ]]; then
  # Expand each arg as a glob (or literal) against TRIPS_ROOT. Patterns
  # like '2024-*' come in as a single quoted string and get expanded here.
  declare -A SEEN=()
  shopt -s nullglob
  for pat in "${TRIP_ARGS[@]}"; do
    matched=0
    for dir in "$TRIPS_ROOT"/$pat; do
      [[ -d "$dir" ]] || continue
      [[ -n "${SEEN[$dir]:-}" ]] && continue
      SEEN[$dir]=1
      TRIPS+=("$dir")
      matched=1
    done
    if [[ $matched -eq 0 ]]; then
      printf '%sERROR%s no trips matched: %s\n' "$C_ERR" "$C_RESET" "$pat"; exit 1
    fi
  done
  shopt -u nullglob
  # Sort for deterministic order across patterns.
  IFS=$'\n' TRIPS=($(printf '%s\n' "${TRIPS[@]}" | sort)); unset IFS
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
printf 'Status:  %s  (Ctrl+T for live progress)\n' "$STATUS_FILE"
PROMOTE_FLAGS=()
[[ $DRY_RUN -eq 1 ]] && PROMOTE_FLAGS+=(--dry-run)

TOTAL_OK=0; TOTAL_FAIL=0; TOTAL_SKIP=0

# --- Live progress (Ctrl+T / SIGINFO) -------------------------------
# Updated before each trip / phase; read by the trap and mirrored to
# $STATUS_FILE so a second terminal can `cat` it without disturbing
# the running shell.
RUN_START_TS=$(date +%s)
CURRENT_TRIP=""
CURRENT_PHASE="init"
CURRENT_INDEX=0
TRIP_START_TS=$RUN_START_TS
TRIP_FILES=0
TRIP_SIZE_H=""
TRIP_BYTES=0

fmt_dur() {
  local s=$1
  printf '%dh%02dm%02ds' $((s/3600)) $(((s%3600)/60)) $((s%60))
}

write_status() {
  local now total done pct trip_el run_el
  now=$(date +%s)
  total=${#TRIPS[@]}
  done=$((CURRENT_INDEX > 0 ? CURRENT_INDEX - 1 : 0))
  pct=0
  [[ $total -gt 0 ]] && pct=$((done * 100 / total))
  trip_el=$((now - TRIP_START_TS))
  run_el=$((now - RUN_START_TS))
  {
    printf 'trip:        %s\n' "${CURRENT_TRIP:-(none)}"
    if [[ $TRIP_FILES -gt 0 || -n "$TRIP_SIZE_H" ]]; then
      printf 'trip size:   %s files, %s\n' "$TRIP_FILES" "${TRIP_SIZE_H:-?}"
    fi
    printf 'phase:       %s\n' "$CURRENT_PHASE"
    printf 'trips:       %d/%d done  (on trip %d, %d%%)\n' "$done" "$total" "$CURRENT_INDEX" "$pct"
    printf 'trip time:   %s\n' "$(fmt_dur "$trip_el")"
    printf 'run time:    %s\n' "$(fmt_dur "$run_el")"
    printf 'tally:       %d ok, %d failed, %d skipped\n' \
      "$TOTAL_OK" "$TOTAL_FAIL" "$TOTAL_SKIP"
    printf 'log:         %s\n' "$RUN_LOG"
    printf 'updated:     %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
  } > "$STATUS_FILE.tmp" && mv "$STATUS_FILE.tmp" "$STATUS_FILE"
}

print_progress() {
  write_status
  printf '\n%s[status]%s ' "$C_OK" "$C_RESET" >&2
  sed 's/^/           /' "$STATUS_FILE" | sed '1s/^ *//' >&2
  # Parse the run log locally for rsync's own progress signal — no ssh
  # round-trip. `--itemize-changes` emits one `>f...`/`<f...` line per
  # file at transfer start, and `--progress` emits the per-file byte
  # counter. Files-started is itemize count; current pct is the last
  # progress line for the file in flight.
  if [[ -n "$CURRENT_TRIP" && -f "$RUN_LOG" ]]; then
    local files_started cur_pct cur_line
    # Tail keeps this O(constant) regardless of log length.
    files_started=$(grep -cE '^[<>][fdL]' "$RUN_LOG" 2>/dev/null || echo 0)
    # Scope to THIS trip: subtract the lines that were already in the shared
    # log when the trip began (set in the loop above; 0 before the first trip).
    files_started=$(( files_started - ${TRIP_ITEMIZE_BASE:-0} ))
    [[ $files_started -lt 0 ]] && files_started=0
    cur_line=$(grep -E '^[[:space:]]+[0-9,]+[[:space:]]+[0-9]+%' "$RUN_LOG" 2>/dev/null | tail -1)
    cur_pct=$(printf '%s\n' "$cur_line" | awk '{for(i=1;i<=NF;i++) if($i~/%$/){gsub("%","",$i); print $i; exit}}')
    if [[ "${files_started:-0}" -gt 0 || -n "$cur_pct" ]]; then
      local pct_files=0
      [[ "${TRIP_FILES:-0}" -gt 0 ]] && pct_files=$((files_started * 100 / TRIP_FILES))
      printf '%s[rsync]%s    files started: %s/%s (%d%%)' \
        "$C_OK" "$C_RESET" "${files_started:-0}" "${TRIP_FILES:-?}" "$pct_files" >&2
      [[ -n "$cur_pct" ]] && printf ', current file: %s%%' "$cur_pct" >&2
      printf '\n' >&2
    fi
  fi
  # Surface the in-process heartbeat written by `immy` itself: shows
  # which file inside the trip is currently being worked on, plus the
  # phase (exif / derivatives / clip / faces / transcript / caption /
  # rsync originals / stack insta360 pairs / album sync). No trip yet
  # = nothing to show.
  if [[ -n "$CURRENT_TRIP" ]]; then
    local hb="$TRIPS_ROOT/$CURRENT_TRIP/.audit/.progress"
    if [[ -f "$hb" ]]; then
      # Only surface fields not already in [status]: step, detail, and
      # how stale the heartbeat is (rsync doesn't refresh it, so a long
      # stale-time during the rsync phase is expected and informative).
      local hb_now hb_upd hb_age step detail
      hb_now=$(date +%s)
      hb_upd=$(awk -F'"' '/"updated_at":/ {print $4; exit}' "$hb")
      step=$(awk -F'"' '/"step":/ {print $4; exit}' "$hb")
      detail=$(awk -F'"' '/"detail":/ {print $4; exit}' "$hb")
      hb_age=""
      if [[ -n "$hb_upd" ]]; then
        local hb_upd_ts
        hb_upd_ts=$(date -j -f '%Y-%m-%dT%H:%M:%S' "${hb_upd%%.*}" +%s 2>/dev/null || echo 0)
        [[ $hb_upd_ts -gt 0 ]] && hb_age="$(fmt_dur $((hb_now - hb_upd_ts))) stale"
      fi
      if [[ -n "$step$detail" ]]; then
        printf '%s[heartbeat]%s ' "$C_OK" "$C_RESET" >&2
        printf 'step: %s\n' "${step:-?}" >&2
        [[ -n "$detail" ]] && printf '           detail: %s\n' "$detail" >&2
        [[ -n "$hb_age" ]] && printf '           %s\n' "$hb_age" >&2
      fi
    fi
  fi
  printf '\n' >&2
}

# Ctrl+T on macOS sends SIGINFO to the foreground process group. We run
# the immy pipelines in the background and `wait` on them, so bash itself
# stays in the foreground and the trap fires immediately.
trap 'print_progress' INFO

# Initial snapshot so the status file exists before the first trip.
write_status

# Run a pipeline in the background and wait, so bash can service SIGINFO
# (Ctrl+T) while a long immy invocation is running. Returns the pipeline
# exit code (with `pipefail` already set above, this reflects immy's rc).
run_bg() {
  # Subshell so pipefail collapses the pipeline's status into one exit
  # code we can `wait` on. Without the subshell, $! would be tee's PID
  # and we'd lose immy's real rc.
  ( set -o pipefail; "$@" 2>&1 | tee -a "$RUN_LOG" ) &
  local pid=$!
  local rc=0
  # `wait` returns 128+signum if interrupted by a caught signal; in that
  # case the child is still alive and we loop back to wait again.
  while :; do
    if wait "$pid"; then rc=0; break; fi
    rc=$?
    kill -0 "$pid" 2>/dev/null || break
  done
  return "$rc"
}

# caffeinate -dims: keep Mac awake (no display, no idle, no system sleep)
# for the full overnight run. Without this, a closed-lid or idle-sleep
# mid-transfer kills rsync. nice -n 10 keeps interactive apps responsive
# if you check in on the laptop.
trip_idx=0
for trip in "${TRIPS[@]}"; do
  trip_idx=$((trip_idx + 1))
  name="$(basename "$trip")"
  CURRENT_INDEX=$trip_idx
  CURRENT_TRIP="$name"
  TRIP_START_TS=$(date +%s)
  # Baseline of itemized-file lines already in the SHARED run log before this
  # trip starts. print_progress counts itemize lines (`>f…`/`<f…`) in $RUN_LOG,
  # which accumulates across every trip — without subtracting this baseline the
  # count is the whole-run total and "files started" reads e.g. 5605/76 (7375%).
  TRIP_ITEMIZE_BASE=$(grep -cE '^[<>][fdL]' "$RUN_LOG" 2>/dev/null || echo 0)
  # Sample trip size/file count once at trip start so Ctrl+T is cheap.
  # Excludes .audit/ since promote rsyncs originals + .audit derivatives
  # separately; the bulk-of-transfer figure is what's most useful here.
  if [[ -d "$trip" ]]; then
    TRIP_FILES=$(find "$trip" -type f -not -path "*/.audit/*" 2>/dev/null | wc -l | tr -d ' ')
    # KB-units (du semantics differ from apparent-size sums).
    kb=$(du -sk "$trip" 2>/dev/null | awk '{print $1+0}')
    TRIP_BYTES=$(( ${kb:-0} * 1024 ))
    TRIP_SIZE_H=$(awk -v b="${TRIP_BYTES:-0}" 'BEGIN{
      u="BKMGT"; i=1; while(b>=1024 && i<5){b/=1024; i++}
      printf "%.1f%s", b, substr(u,i,1)
    }')
  else
    TRIP_FILES=0; TRIP_BYTES=0; TRIP_SIZE_H=""
  fi

  if [[ $FORCE -eq 0 ]] && is_promoted "$trip"; then
    CURRENT_PHASE="skip (already promoted)"
    write_status
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
    CURRENT_PHASE="audit --write --auto"
    write_status
    if ! run_bg caffeinate -dims nice -n 10 \
           "$IMMY_BIN" audit "$trip" --write --auto --yes-medium; then
      rc=$?
      TOTAL_FAIL=$((TOTAL_FAIL + 1))
      printf '%s[fail rc=%s audit]%s %s\n' \
        "$C_ERR" "$rc" "$C_RESET" "$name" | tee -a "$RUN_LOG"
      continue
    fi
  fi

  CURRENT_PHASE="promote"
  write_status
  if run_bg caffeinate -dims nice -n 10 \
       "$IMMY_BIN" promote "$trip" "${PROMOTE_FLAGS[@]}"; then
    TOTAL_OK=$((TOTAL_OK + 1))
  else
    rc=$?
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

CURRENT_PHASE="done"
write_status

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
