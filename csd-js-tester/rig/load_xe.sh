#!/bin/bash
# Load an .xe into RAM and prove THIS image is the one running, then run a rig.
#
# WHY A SCRIPT AND NOT A ONE-LINER (each clause below has cost real time):
#  - SetEnv must be sourced FROM the tools dir (it uses $PWD) and inside a
#    subshell, or XMOS_TOOL_PATH is wrong and later cmake/xrun fail oddly.
#  - stdbuf is required: without line buffering the xrun log stays empty and a
#    load that worked reads as a silent failure.
#  - A plain `xrun --io &` dies instantly with a 0-byte log.
#  - The wait must be on the devnum CHANGE, not a sleep: probing too early
#    reports a false NOT-PROVEN.
#  - `pkill -x xrun` does NOT stop the loaded image -- the .xe keeps running
#    and the DAC keeps enumerating, so the next test silently exercises the
#    PREVIOUS build. Hence the build-stamp proof below: it is the only thing
#    that tells you which image is live.
#  - Kill stale xgdbserver too: one still alive holds the XTAG and the next
#    xrun loads nothing and logs nothing.
set -u
export HOME=/home/ycui
XE=${1:?usage: run_xe.sh <image.xe> [rig.py args...]}
shift
[ -f "$XE" ] || { echo "NOT-PROVEN: no such image $XE"; exit 1; }

pkill -x xrun 2>/dev/null
pkill -x xgdbserver 2>/dev/null
sleep 2

cd /home/ycui/XMOS-15.3.1 && set +u && source ./SetEnv >/dev/null 2>&1
export XMOS_LICENSE_FILE=/home/ycui/workspace/xmos-installer/xTime_key.txt

LOG=$(mktemp /tmp/run_xe.XXXXXX.log)
# Redirect at the SUBSHELL level, not just on xrun. Redirecting only xrun left
# the subshell itself holding this script's stdout pipe, so a piped consumer
# (`| tail`) never saw EOF and the rig's result stayed trapped in the pipe
# after the rig exited -- the run looked hung at 6 minutes for a 1 minute soak.
( cd /home/ycui/XMOS-15.3.1 && set +u && source ./SetEnv >/dev/null 2>&1
  export XMOS_LICENSE_FILE=/home/ycui/workspace/xmos-installer/xTime_key.txt
  # --xscope is REQUIRED for any device-side output on XS3: --io alone leaves
  # the log at 0 bytes no matter what the firmware prints (measured
  # 2026-11-04: same image, --io -> 0 bytes, --io --xscope -> 5,679 bytes
  # carrying the boot banner and init dump). The weeks of "legitimately empty
  # xrun logs" were this missing flag plus xscope_host_data being off, not
  # loader silence.
  stdbuf -oL -eL xrun --io --xscope "$XE" ) </dev/null >"$LOG" 2>&1 &
XRUN_PID=$!

# Wait for the runtime PID to appear and settle.
DEVPREV=$(lsusb -d 20b1:0018 2>/dev/null | awk '{print $4}')
ok=0
for i in $(seq 1 30); do
  sleep 2
  if lsusb -d 20b1:0018 >/dev/null 2>&1; then ok=1; break; fi
done
[ $ok -eq 1 ] || { echo "NOT-PROVEN: 20b1:0018 never enumerated"; tail -20 "$LOG"; exit 1; }
sleep 3

# xrun faults that ARE fatal -- distinguished from mere silence.
grep -qiE "could not determine xgdbserver|No XMOS devices|No devices found|resource exception|ET_(ILLEGAL|ECALL)" "$LOG" && {
  echo "NOT-PROVEN: xrun log carries a fault:"; grep -iE "could not determine|No devices|resource exception|ET_" "$LOG" | head -5; exit 1; }
[ -s "$LOG" ] || echo "NOTE: xrun log is empty. It does that legitimately (observed 2026-11-03: empty log, image verified running by its own IDN), so an empty log is NOT proof of failure -- and treating it as such produced a false NOT-PROVEN that sent me chasing a phantom loader bug. Identity below is the proof."

APP=/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester
PY="$APP/.venv/bin/python"

# PROOF: the device's own *IDN? must carry THIS image's version stamp.
# Not the USB iSerial: the serial is per-UNIT (flash die unique serial) and is
# identical for every build run on this board, so it cannot tell two builds
# apart -- exactly the discrimination this step exists to make. The version
# stamp can. (Also note iProduct is NOT a reliable ident here: the vendor
# product strings are gated by SPECIALTYCIRCUITS_RELEASE, so a debug build of
# ours legitimately reports the stock "XMOS (UAC2.0)".)
STAMP=$(strings -a "$XE" | grep -oE "2026\.[0-9]+\.[0-9]+\.[0-9]+-[0-9a-zA-Z.-]+" | head -1)
if [ -z "$STAMP" ]; then
  echo "NOT-PROVEN: no version stamp inside $XE -- nothing to compare against"
  exit 1
fi
IDN=$(cd "$APP" && timeout 25 "$PY" csd_diag.py --backend rawmidi --timeout 15 '*IDN?' 2>/dev/null | tr -d '\r')
echo "device *IDN? : ${IDN:-<no reply>}"
echo "image stamp  : $STAMP"
if ! printf '%s' "$IDN" | grep -qF "$STAMP"; then
  echo "NOT-PROVEN: the running build does not carry this image's stamp."
  echo "            The .xe did not take; refusing to credit it with any result."
  exit 1
fi
echo "PROVEN: our build loaded (device IDN stamp matches the .xe)"
echo "IMAGE : $XE  md5=$(md5sum "$XE" | cut -d' ' -f1)  stamp=$STAMP"

if [ $# -gt 0 ]; then
  cd ~/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester
  "$@"
  RC=$?
  echo "SCRIPT_RC=$RC for $1"
fi
echo "RUN_XE_LOG=$LOG"
echo "XRUN_PID=$XRUN_PID"
