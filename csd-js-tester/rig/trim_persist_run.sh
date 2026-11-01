#!/bin/bash
# Two-phase persistence rig: arm, reload the firmware, verify.
#   usage: rig/trim_persist_run.sh /path/to/file.xe
#
# WHY A WRAPPER
# Persistence can only be observed across a firmware restart, and the load is
# owned by run_xe.sh (it starts xrun, proves the build came up, runs one
# script, then kills xrun). So the reload is simply: finish the arm pass, let
# run_xe.sh tear xrun down, and start a SECOND run_xe.sh for the verify pass.
# One script cannot do both halves -- in a single life the value it set is
# still in RAM, so reading it back proves nothing.
set -u
S=/home/ycui/.hermes/cache/scratch
HERE=$(cd "$(dirname "$0")" && pwd)
XE=${1:?usage: trim_persist_run.sh /path/to/file.xe}
[ -f "$XE" ] || { echo "NO-SUCH-ARTIFACT: $XE"; exit 1; }

echo "########## PHASE 1: arm (sets trim, journals it) ##########"
PERSIST_MODE=arm bash $S/run_xe.sh "$XE" rig/trim_persist_check.py 2>&1 \
  | grep -avE "^\[|WARN|xrun --"
rc=${PIPESTATUS[0]}
echo "ARM_RC=$rc"
# run_xe.sh exits with the rig's code: 3 = armed and awaiting reload.
[ "$rc" = "3" ] || { echo "ARM-DID-NOT-ARM (rc=$rc) -- refusing to call verify a pass"; exit 1; }

echo
echo "########## xrun down; board idle 4s so the store task finishes ##########"
pkill -x xrun; sleep 4

echo
echo "########## PHASE 2: verify (fresh firmware load reads flash) ##########"
PERSIST_MODE=verify bash $S/run_xe.sh "$XE" rig/trim_persist_check.py 2>&1 \
  | grep -avE "^\[|WARN|xrun --"
rc2=${PIPESTATUS[0]}
echo "VERIFY_RC=$rc2"
[ "$rc2" = "0" ] && echo "PERSISTENCE: PASS" || echo "PERSISTENCE: FAIL (rc=$rc2)"
exit $rc2
