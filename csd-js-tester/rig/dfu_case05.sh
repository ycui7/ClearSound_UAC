#!/bin/bash
# Case 05, attempt 5 — the layout is now explicit end to end.
#   QSPI part            2 MiB total
#   boot partition       1 MiB (factory image + the 4 KiB data/journal)
#   upgrade partition    512 KiB  <- what the DFU write was actually hitting
#                         (0.48 x 1,052,672 ~= 505 KiB; 512 KiB boundary)
#   upgrade image        built at exactly 524288 bytes, --factory-version 15.3
# Then: flash the factory with the same declared upgrade size, and DFU twice.
set +u
REPO=$HOME/sw-audio-sc/sw_usb_audio/clearsound_uac
XE=$REPO/bin/2AMi2o2xxxxxd/ClearSoundDac_2AMi2o2xxxxxd.xe
TRIM=$HOME/workspace/csd-firmware-archive/trim_journal_erased_4k.bin
IMG=/tmp/u153.bin
BPART=1048576
UPART=524288
PID="20b1:0018"
say() { echo; echo "### $*"; }

source "$HOME/XMOS/XTC/15.3.0/SetEnv" </dev/null >/dev/null 2>&1
export XMOS_TOOL_PATH="$HOME/XMOS/XTC/15.3.0" XMOS_HOME="$HOME/.xmos"
export XMOS_CMAKE_PATH="$XMOS_TOOL_PATH/build/xcommon_cmake"
export XCC_LIBRARY_PATH="$XMOS_TOOL_PATH/target/lib"
export XCC_C_INCLUDE_PATH="$XMOS_TOOL_PATH/target/include:$XMOS_TOOL_PATH/target/include/clang"
export XCC_XC_INCLUDE_PATH="$XMOS_TOOL_PATH/target/include/xc:$XCC_C_INCLUDE_PATH"
export XCC_CPLUS_INCLUDE_PATH="$XCC_C_INCLUDE_PATH:$XMOS_TOOL_PATH/target/include/c++/v1"
export XCC_ASSEMBLER_INCLUDE_PATH="$XCC_C_INCLUDE_PATH"
export XCC_DEVICE_PATH="$XMOS_HOME/targets:$XMOS_TOOL_PATH/configs:$XMOS_TOOL_PATH/configs/.deprecated"
export XCC_TARGET_PATH="$XMOS_HOME/targets:$XMOS_TOOL_PATH/targets:$XMOS_TOOL_PATH/targets/.deprecated"
export PATH="$XMOS_TOOL_PATH/bin:$PATH"
cd /home/ycui

say "upgrade image at $UPART bytes"
xflash -o "$IMG" --factory-version 15.3 --boot-partition-size $BPART --upgrade 1 "$XE" $UPART >/tmp/dfu5_img.log 2>&1
echo "image rc=$? size=$(stat -c %s "$IMG" 2>/dev/null || echo missing)"
grep -iE 'error|too |large' /tmp/dfu5_img.log | tail -2

say "factory flash declaring the same upgrade size"
pkill -x xrun; sleep 2
xflash -f --boot-partition-size $BPART --factory "$XE" --upgrade 1 "$XE" $UPART --data "$TRIM" >/tmp/dfu5_rec.log 2>&1
echo "recover rc=$? ok=$(grep -c 'has finished successfully' /tmp/dfu5_rec.log)"
grep -iE 'error|too small|not enough' /tmp/dfu5_rec.log | tail -3
sleep 10

cd "$REPO/csd-js-tester" || exit 9
export PYTHONPATH=/home/ycui/.cache/uv/archive-v0/4MSAPDQVJdTuWpErp
PY=./.venv/bin/python
idn() { $PY -c "import sys;sys.path.insert(0,'.');from rig.csd_suite import scpi,find_dac;print(scpi('*IDN?',find_dac())[1].strip('\"'))" 2>/dev/null; }
echo "before: $(idn)"

leg() {
  say "leg $1: detach"; dfu-util -d $PID -e 2>&1 | grep -aE 'detach|Found DFU' | tail -2
  for i in $(seq 1 20); do dfu-util -l 2>&1 | grep -q 'Found DFU' && break; sleep 0.5; done
  say "leg $1: write"; dfu-util -d $PID -D "$IMG" -R 2>&1 | grep -aE 'dfuERROR|status\(|Done|failed' | tail -3
  say "leg $1: back up?"
  for i in $(seq 1 45); do
    sleep 1; OUT=$(idn)
    if echo "$OUT" | grep -q ClearSound; then echo "answered: $OUT"; return 0; fi
  done
  echo "NO MIDI ANSWER after 45 s"; return 1
}
leg 1 || { say "recovering after leg 1"; xflash -f --boot-partition-size $BPART --factory "$XE" --upgrade 1 "$XE" $UPART --data "$TRIM" >/tmp/dfu5_rec2.log 2>&1; echo "recover rc=$?"; exit 4; }
leg 2 || { say "recovering after leg 2"; xflash -f --boot-partition-size $BPART --factory "$XE" --upgrade 1 "$XE" $UPART --data "$TRIM" >/tmp/dfu5_rec3.log 2>&1; echo "recover rc=$?"; exit 5; }
say "case 05: two consecutive DFU flashes completed"
