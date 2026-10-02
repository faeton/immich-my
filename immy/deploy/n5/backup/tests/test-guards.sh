#!/usr/bin/env bash
# Unit test for guards-lib.sh: counting failures must fail closed (non-zero, no
# number on stdout). Local-only; stubs ssh and find. Run: bash tests/test-guards.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../guards-lib.sh"
check() { # name expected_rc expected_out actual_rc actual_out
  if [ "$2" = nz ]; then [ "$4" -ne 0 ] || { echo "FAIL $1: rc 0"; echo x >> "$FAILF"; return; }
  else [ "$4" -eq "$2" ] || { echo "FAIL $1: rc $4"; echo x >> "$FAILF"; return; }; fi
  [ "$5" = "$3" ] || { echo "FAIL $1: out '$5' != '$3'"; echo x >> "$FAILF"; return; }
  echo "ok   $1"
}
T="$(mktemp -d)"; FAILF="$T/fails"; : > "$FAILF"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/full/sub" "$T/empty"; touch "$T/full/a" "$T/full/sub/b"

o=$(count_files "$T/full" 1); check count-capped 0 1 $? "$o"
o=$(count_files "$T/full" 10); check count-all 0 2 $? "$o"
o=$(count_files "$T/empty" 1); check count-empty 0 0 $? "$o"
o=$(count_files "$T/nope" 1 2>/dev/null); check count-missing-dir nz "" $? "$o"
o=$(count_files "$T/full" abc 2>/dev/null); check count-bad-max nz "" $? "$o"
( find() { return 1; }; o=$(count_files "$T/full" 1 2>/dev/null); check count-find-fails nz "" $? "$o" )
( find() { echo .; return 2; }; o=$(count_files "$T/full" 1 2>/dev/null); check count-find-partial-error nz "" $? "$o" )

SSH_CMD=ssh REMOTE=vv
( ssh() { return 255; }; o=$(remote_state /x 2>/dev/null); check ssh-255 nz "" $? "$o" )
( ssh() { echo garbage; return 0; }; o=$(remote_state /x 2>/dev/null); check ssh-garbage nz "" $? "$o" )
( ssh() { return 0; }; o=$(remote_state /x 2>/dev/null); check ssh-silent-success nz "" $? "$o" )
( ssh() { echo YES; }; o=$(remote_state /x); check remote-yes 0 YES $? "$o" )
( ssh() { echo NO; }; o=$(remote_state /x); check remote-no 0 NO $? "$o" )
( ssh() { echo MISSING; }; o=$(remote_state /x); check remote-missing 0 MISSING $? "$o" )
[ ! -s "$FAILF" ]
