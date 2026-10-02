#!/usr/bin/env bash
# Fail-closed counting helpers for nightly-mirror.sh (sourced; also unit-tested
# by tests/test-guards.sh). Contract: on ANY error (find/ssh failure, garbage
# output) print nothing on stdout, a reason on stderr, and return non-zero.
# Callers must treat a failure as "abort the run", never as 0 / "empty".

# count_files DIR MAX -> prints min(#regular files under DIR, MAX).
count_files() {
  local dir="$1" max="$2" out rcs rc_find rc_head
  [[ "$max" =~ ^[0-9]+$ ]] || { echo "count_files: bad max '$max'" >&2; return 2; }
  # Capture BOTH pipeline statuses right after the pipeline, before anything else runs.
  out="$(find "$dir" -type f -printf '.\n' 2>/dev/null | head -n "$max"; echo "rc=${PIPESTATUS[0]},${PIPESTATUS[1]}")" || return 1
  rcs="${out##*rc=}"
  out="${out%rc=*}"
  rc_find="${rcs%%,*}"; rc_head="${rcs##*,}"
  [[ "$rc_find" =~ ^[0-9]+$ && "$rc_head" =~ ^[0-9]+$ ]] \
    || { echo "count_files: could not read pipeline status for $dir" >&2; return 1; }
  [ "$rc_head" = 0 ] || { echo "count_files: head failed (rc=$rc_head) on $dir" >&2; return 1; }
  # 141 = find killed by SIGPIPE because head had enough: fine (head succeeded).
  if [ "$rc_find" != 0 ] && [ "$rc_find" != 141 ]; then
    echo "count_files: find failed (rc=$rc_find) on $dir" >&2; return 1
  fi
  printf '%s' "$out" | wc -l | tr -d ' \n' | grep -E '^[0-9]+$' \
    || { echo "count_files: non-numeric count for $dir" >&2; return 1; }
}

# remote_state REMOTE_DIR -> prints exactly one of MISSING | NO | YES (has files).
# Uses $SSH_CMD and $REMOTE (as in nightly-mirror.sh).
remote_state() {
  local dir="$1" out
  # shellcheck disable=SC2086
  out="$($SSH_CMD "$REMOTE" "[ -d '$dir' ] || { echo MISSING; exit 0; }; x=\$(find '$dir' -type f -print -quit) || exit 3; if [ -n \"\$x\" ]; then echo YES; else echo NO; fi")" \
    || { echo "remote_state: ssh/find failed for $dir" >&2; return 1; }
  case "$out" in
    MISSING|NO|YES) printf '%s\n' "$out" ;;
    *) echo "remote_state: unexpected reply '$out' for $dir" >&2; return 1 ;;
  esac
}
