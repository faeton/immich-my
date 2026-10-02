#!/usr/bin/env bash
# Fail-closed counting helpers for nightly-mirror.sh (sourced; also unit-tested
# by tests/test-guards.sh). Contract: on ANY error (find/ssh failure, garbage
# output) print nothing on stdout, a reason on stderr, and return non-zero.
# Callers must treat a failure as "abort the run", never as 0 / "empty".

# mirror_exclude_dirs KIND -> directory names (one per line) that the KIND
# tree's rsync excludes (anywhere in the tree). KIND: originals|media|state.
# Single source for both the rsync --exclude args and the empty-source guard's
# count, so the guard counts exactly what will be transferred.
#   .rsync-partial — rsync's own --partial-dir, everywhere.
#   .audit         — originals ONLY: per-trip immy bookkeeping that a Mac-layout
#                    trip keeps next to its originals. On the NAS all immy state
#                    lives under <state_root>/<trip>/.audit/, so excluding .audit
#                    from the state tree would mirror nothing at all.
mirror_exclude_dirs() {
  case "${1:-}" in
    originals)   printf '%s\n' .rsync-partial .audit ;;
    media|state) printf '%s\n' .rsync-partial ;;
    *) echo "mirror_exclude_dirs: unknown tree kind '${1:-}'" >&2; return 2 ;;
  esac
}

# count_files DIR MAX [EXCLUDED_DIR_NAME...] -> prints min(#regular files under
# DIR, MAX), not descending into directories with any of the given names (the
# same set rsync excludes for that tree; see mirror_exclude_dirs).
count_files() {
  local dir="$1" max="$2" out rcs rc_find rc_head
  shift 2 || { echo "count_files: usage DIR MAX [EXCLUDED_DIR...]" >&2; return 2; }
  [[ "$max" =~ ^[0-9]+$ ]] || { echo "count_files: bad max '$max'" >&2; return 2; }
  local -a prune=()
  local name
  for name in "$@"; do
    [ ${#prune[@]} -eq 0 ] || prune+=(-o)
    prune+=(-name "$name")
  done
  local -a expr=()
  [ ${#prune[@]} -eq 0 ] || expr=(\( -type d \( "${prune[@]}" \) \) -prune -o)
  # Capture BOTH pipeline statuses right after the pipeline, before anything else runs.
  out="$(find "$dir" "${expr[@]}" -type f -printf '.\n' 2>/dev/null | head -n "$max"; echo "rc=${PIPESTATUS[0]},${PIPESTATUS[1]}")" || return 1
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
