#!/bin/bash
# Build and run the function-generator host gate.
#
#   usage: tools/dsp/test_gen.sh
#
# Exits non-zero if the generator's waveforms do not match their specification.
# Same pattern as tools/dsp/test_gates.sh: asserts on real compiled code, run
# before anything reaches the target.
#
# WHY THE BINARY IS DELETED FIRST AND THE COMPILE STATUS CHECKED EXPLICITLY
# A previous version piped g++ through `head` to trim the output. head exits 0,
# so `g++ ... | head && ./test_gen` ran the PREVIOUS binary after a failed
# compile and printed its verdict as if it were the new one. That is the exact
# class of false-green this harness exists to prevent, so the build is now
# unconditional-remove, status-checked, and the binary is verified present
# before it is executed.
set -u
# APP is overridable so tools/dsp/test_guards.sh can point the whole gate at a
# scratch COPY of the tree. It used to be hard-coded, which left the guards no
# option but to mutate the real src/extensions/*.h in place -- and an
# in-place mutation that is interrupted leaves a corrupted generated table in
# the working tree, one `git add .` from being committed.
: "${APP:=$HOME/sw-audio-sc/sw_usb_audio/clearsound_uac}"
SRC=$APP/tools/dsp/test_gen.cpp
BIN=/tmp/test_gen.$$
TABLES=$APP/src/extensions/csd_gen_tables.h
CORE=$APP/src/extensions/csd_gen_core.h
: "${PY:=/home/ycui/.hermes/hermes-agent/venv/bin/python}"
# Per-run build log: two gate runs in flight used to share /tmp/gen_build.log,
# so a guard could print another run's compiler output as its own.
BUILD_LOG=/tmp/gen_build.$$

cd "$APP" || exit 1
fail=0

# T0: the tables are generated, so check the generator still reproduces them
# and that its own self-check passes. A hand-edited table file fails here.
if [ -f "$TABLES" ]; then
    cp "$TABLES" /tmp/csd_gen_tables.committed.$$
    if $PY tools/dsp/gen_gen_tables.py /tmp/csd_gen_tables.regen.$$ >/tmp/gen_tables.log 2>&1; then
        if cmp -s /tmp/csd_gen_tables.committed.$$ /tmp/csd_gen_tables.regen.$$; then
            echo "T0 PASS: tables reproduce byte-identically, generator self-check OK"
        else
            echo "T0 FAIL: $TABLES differs from what the generator produces"
            echo "         (hand edit, or generator changed without regenerating)"
            fail=1
        fi
    else
        echo "T0 FAIL: table generator self-check rejected the tables:"
        sed -n '1,10p' /tmp/gen_tables.log
        fail=1
    fi
    rm -f /tmp/csd_gen_tables.committed.$$ /tmp/csd_gen_tables.regen.$$ "$BUILD_LOG"
else
    echo "T0 FAIL: $TABLES missing -- run tools/dsp/gen_gen_tables.py"
    fail=1
fi

# Build. No pipe on the compiler: its exit status must reach us.
rm -f "$BIN"
g++ -O2 -std=c++11 -Wall -Wextra -Werror=return-type \
    -I"$APP/src/extensions" "$SRC" -o "$BIN" >"$BUILD_LOG" 2>&1
rc=$?
if [ $rc -ne 0 ]; then
    echo "GEN HOST GATE: BUILD FAILED (rc=$rc)"
    sed -n '1,40p' "$BUILD_LOG"
    exit 1
fi
if [ ! -x "$BIN" ]; then
    echo "GEN HOST GATE: BUILD FAILED (compiler exited 0 but produced no binary)"
    exit 1
fi
# Compile warnings are information, not failure: -Wall on a DSP test harness
# would fail on unused-argument noise and get the warnings switched off.
if [ -s "$BUILD_LOG" ]; then
    echo "--- compiler warnings ---"
    sed -n "1,15p" "$BUILD_LOG"
fi

echo "--- running gate (binary built from source at $(date -r "$CORE" '+%H:%M:%S')) ---"
if [ "$CORE" -nt "$BIN" ] || [ "$SRC" -nt "$BIN" ]; then
    echo "GEN HOST GATE: REFUSING -- source newer than the binary just built"
    exit 1
fi

"$BIN"
rc=$?
rm -f "$BIN"

# Both halves must agree. This line was `exit $rc`, which threw T0's result
# away: the guard printed "T0 FAIL" and then exited 0, so tools/dsp/test_guards.sh
# reported "hand-edited table detected by T0: FAIL (gate passed)" for a file
# T0 had correctly rejected. A check whose verdict is printed and then dropped
# is the fourth instance in this project of a guard that silently does
# nothing, and -- like the others -- it looked exactly like a passing check.
if [ $rc -ne 0 ] || [ $fail -ne 0 ]; then
    [ $fail -ne 0 ] && echo "GEN HOST GATE: FAIL (table provenance, see T0 above)"
    [ $rc -ne 0 ]   && echo "GEN HOST GATE: FAIL ($rc from the assertions)"
    exit 1
fi
echo "GEN HOST GATE: PASS"
exit 0
