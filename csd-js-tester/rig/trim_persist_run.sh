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
#
# WHY THE VERDICT IS PARSED FROM THE OUTPUT AND NOT TAKEN FROM $?
# run_xe.sh's final statement is `echo "SCRIPT_RC=$RC ..."`, and an echo
# returns 0, so run_xe.sh ALWAYS exits 0 no matter what the rig decided --
# verified by reading its last lines, not assumed. `${PIPESTATUS[0]}` therefore
# reports success for a rig that failed outright, which is precisely a false
# PASS. The rig's real verdict is the SCRIPT_RC line, so that is parsed, and a
# missing line is treated as failure rather than as zero. (The previous
# revision used PIPESTATUS and could never reach phase 2 at all -- it failed
# safe, but it could never pass either.)
set -u
S=/home/ycui/.hermes/cache/scratch
HERE=$(cd "$(dirname "$0")" && pwd)
XE=${1:?usage: trim_persist_run.sh /path/to/file.xe}
[ -f "$XE" ] || { echo "NO-SUCH-ARTIFACT: $XE"; exit 1; }
LOGDIR=${TMPDIR:-/tmp}

rig_rc_from_log() {                      # $1 = log file; echoes rc or X
  local v
  v=$(grep -aoE 'SCRIPT_RC=[0-9]+' "$1" 2>/dev/null | tail -1 | cut -d= -f2)
  [ -n "$v" ] && echo "$v" || echo X
}

echo "########## PHASE 1: arm (sets trim, journals it) ##########"
LOG1=$LOGDIR/trim_arm.log
PERSIST_MODE=arm bash $S/run_xe.sh "$XE" rig/trim_persist_check.py >"$LOG1" 2>&1
rc=$(rig_rc_from_log "$LOG1")
grep -avE "^\[|WARN|xrun --" "$LOG1" | tail -50
echo "ARM_RC=$rc"
if [ "$rc" != "3" ]; then
  echo "ARM-DID-NOT-ARM (rc=$rc, expected 3) -- refusing to run verify"
  echo "  full log: $LOG1"
  exit 1
fi

echo
echo "########## xrun down; idle 4s so any journal write finishes ##########"
pkill -x xrun 2>/dev/null; sleep 4

echo
echo "########## PHASE 2: verify (fresh firmware load reads flash) ##########"
LOG2=$LOGDIR/trim_verify.log
PERSIST_MODE=verify bash $S/run_xe.sh "$XE" rig/trim_persist_check.py >"$LOG2" 2>&1
rc2=$(rig_rc_from_log "$LOG2")
grep -avE "^\[|WARN|xrun --" "$LOG2" | tail -70
echo "VERIFY_RC=$rc2"
if [ "$rc2" = "X" ]; then
  echo "PERSISTENCE: FAIL (no SCRIPT_RC line -- the verify rig never finished)"
  echo "  full log: $LOG2"
  exit 1
fi
[ "$rc2" = "0" ] && echo "PERSISTENCE: PASS" || echo "PERSISTENCE: FAIL (rc=$rc2)"
exit "$rc2"
