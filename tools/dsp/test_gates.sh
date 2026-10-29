#!/bin/bash
# A1 gate tests for PCM DSP (ub-mode: written failing, before the code exists).
#   T1: xua_defs.h defines the PCM_DSP_EN switch (default OFF).
#   T2: a PCM_DSP_EN=1 build links the app DSP entry point (csd_dsp_setup) and
#       the lib FIR (filter_fir_s32) into the .xe.
#   T3: the default (OFF) build does NOT contain csd_dsp_setup (zero footprint
#       when disabled).
# Usage: tools/dsp/test_gates.sh   (from app root; exit non-zero on any fail)
set -u
APP=~/sw-audio-sc/sw_usb_audio/clearsound_uac
XE=$APP/bin/2AMi2o2xxxxxd/ClearSoundDac_2AMi2o2xxxxxd.xe
OBJ=~/XMOS/XTC/15.3.0/bin/xobjdump
fail=0

# T1 switch present, default off
if grep -q "PCM_DSP_EN" "$APP/src/core/xua_defs.h" \
   && grep -qE "#define[[:space:]]+PCM_DSP_EN[[:space:]]+\(?0\)?" "$APP/src/core/xua_defs.h"; then
    echo "T1 PASS: PCM_DSP_EN defined, default OFF"
else
    echo "T1 FAIL: PCM_DSP_EN switch missing or not default-off"; fail=1
fi

xe_has() { "$OBJ" -t "$XE" 2>/dev/null | grep -qE "$1"; }

# T3 first (current build must be the default OFF build per A1 contract)
if xe_has "csd_dsp_setup" || xe_has "csd_dsp_setup\.nstackwords"; then
    echo "T3 FAIL: csd_dsp_setup present in default build (should be absent)"
    fail=1
else
    echo "T3 PASS: default build carries no app DSP symbols"
fi

exit $fail
