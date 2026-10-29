#!/bin/bash
# DSP gate tests (ub-mode: written failing, before the code existed).
#   T1: xua_defs.h defines PCM_DSP_EN (default OFF).
#   T2: a PCM_DSP_EN=1 build links the app DSP frame function (csd_dsp_frame).
#   T3: the default (OFF) build does NOT contain it (zero footprint).
#   T4: coefficient selftest passes (gen_coeffs.py --selftest, host model).
# Run from app root. Rebuilds both configs; asserts on the produced .xe.
set -u
APP=~/sw-audio-sc/sw_usb_audio/clearsound_uac
XE=$APP/bin/2AMi2o2xxxxxd/ClearSoundDac_2AMi2o2xxxxxd.xe
OBJ=~/XMOS/XTC/15.3.0/bin/xobjdump
PY=/home/ycui/.hermes/hermes-agent/venv/bin/python
fail=0

cd "$APP" || exit 1

# T1 switch present, default off
if grep -qE "#define[[:space:]]+PCM_DSP_EN[[:space:]]+\(?0\)?" src/core/xua_defs.h; then
    echo "T1 PASS: PCM_DSP_EN defined, default OFF"
else
    echo "T1 FAIL: PCM_DSP_EN switch missing or not default-off"; fail=1
fi

# T4 coefficient spec (float + Q31 quantized re-check, all rates)
if $PY tools/dsp/gen_coeffs.py --selftest >/tmp/dsp_selftest.log 2>&1; then
    echo "T4 PASS: coefficient selftest (8 rates, Q31 re-check)"
else
    echo "T4 FAIL: coefficient selftest — see /tmp/dsp_selftest.log"; fail=1
fi

# T5 ring core host test (same header the firmware compiles)
if g++ -O1 -std=c++11 tools/dsp/test_ring.cpp -o /tmp/test_ring 2>/dev/null \
   && /tmp/test_ring >/tmp/dsp_ring.log 2>&1; then
    echo "T5 PASS: ring core exact D-delay (all rate tap counts)"
else
    echo "T5 FAIL: ring core test — see /tmp/dsp_ring.log"; fail=1
fi

# T2/T3 build matrix (OFF clean, ON links the frame function)
bash ~/.hermes/cache/scratch/dsp_build.sh >/tmp/dsp_matrix.log 2>&1
grep -q "T2 PASS: csd_dsp_frame linked" /tmp/dsp_matrix.log && echo "T2 PASS: ON build links csd_dsp_frame" \
    || { echo "T2 FAIL: csd_dsp_frame not linked in ON build — see /tmp/dsp_matrix.log"; fail=1; }
grep -q "T3 PASS: OFF build clean" /tmp/dsp_matrix.log && echo "T3 PASS: OFF build carries no DSP symbols" \
    || { echo "T3 FAIL: OFF build not clean — see /tmp/dsp_matrix.log"; fail=1; }

exit $fail
