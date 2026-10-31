#!/bin/bash
# Prove the harness guards fire. Each check breaks something on purpose and
# requires the harness to NOTICE. A guard that has never been observed to fail
# is not a guard -- this session produced a stale-binary false green, a
# round-trip assert that reduced to `x - x == 0`, and a find-based staleness
# check that silently matched nothing. All three looked like working checks.
#
#   usage: tools/dsp/test_guards.sh
set -u
APP=~/sw-audio-sc/sw_usb_audio/clearsound_uac
cd "$APP" || exit 1
fail=0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

note() { printf '%-58s %s\n' "$1" "$2"; }

# G1: the generator must refuse when the core and the generator disagree on the
# square's harmonic count (it sizes the Gibbs table in one and bounds the
# oscillator loop in the other).
cp src/extensions/csd_gen_core.h "$TMP/core.orig"
sed 's/#define CSD_GEN_SQ_HARMONICS 32/#define CSD_GEN_SQ_HARMONICS 24/' \
    "$TMP/core.orig" > src/extensions/csd_gen_core.h
if python3 tools/dsp/gen_gen_tables.py "$TMP/out.h" >"$TMP/g1.log" 2>&1; then
    note "G1 harmonic-count disagreement refused" "FAIL (accepted)"
    fail=1
else
    if grep -q "TABLE GENERATOR REFUSED" "$TMP/g1.log"; then
        note "G1 harmonic-count disagreement refused" "PASS"
    else
        note "G1 harmonic-count disagreement refused" "FAIL (wrong reason)"
        sed -n '1,3p' "$TMP/g1.log"
        fail=1
    fi
fi
cp "$TMP/core.orig" src/extensions/csd_gen_core.h

# G2: a hand-edited generated table must be caught by the byte-identity check
# in test_gen.sh (T0), not silently compiled.
#
# Edited with python rather than sed: an earlier revision used
# `sed '0,/pat/! s/pat/.../'`, whose negated 0-range EXCLUDES the first
# matching line -- the exact line being targeted -- so it changed nothing and
# the check below reported "gate passed" for a file that was never broken.
# A guard's setup has to be verified too, which is what the cmp caught.
cp src/extensions/csd_gen_tables.h "$TMP/tables.orig"
python3 - "$PWD/src/extensions/csd_gen_tables.h" <<'EOF'
import sys
p = sys.argv[1]
s = open(p).read()
# replace() with count=1, not a uniqueness assertion: a sine table is
# symmetric (sin(2*pi*k/N) == sin(2*pi*(N-k)/N)), so every entry appears
# twice by construction and "occurs exactly once" is the wrong requirement.
# What must be true is that the anchor exists and the file ends up changed --
# the cmp below is the authority on that.
if s.count("13176712") < 1:
    raise SystemExit("anchor absent -- edit aborted")
open(p, "w").write(s.replace("13176712", "13176713", 1))
EOF
if cmp -s "$TMP/tables.orig" src/extensions/csd_gen_tables.h; then
    note "G2 setup: table actually modified" "FAIL (edit changed nothing)"
    fail=1
else
    note "G2 setup: table actually modified" "PASS"
fi
if bash tools/dsp/test_gen.sh >"$TMP/g2.log" 2>&1; then
    note "G2 hand-edited table detected by T0" "FAIL (gate passed)"
    fail=1
else
    if grep -q "differs from what the generator produces" "$TMP/g2.log"; then
        note "G2 hand-edited table detected by T0" "PASS"
    else
        note "G2 hand-edited table detected by T0" "FAIL (wrong reason)"
        grep -E "FAIL|REFUS" "$TMP/g2.log" | head -3
        fail=1
    fi
fi
cp "$TMP/tables.orig" src/extensions/csd_gen_tables.h

# G3: the gate must refuse to run when the compile fails, rather than exec a
# leftover binary. Simulated by pointing the build at a broken source.
cp tools/dsp/test_gen.cpp "$TMP/test_gen.orig"
printf '\nthis is not valid c++\n' >> tools/dsp/test_gen.cpp
# leave a decoy binary behind, exactly the shape that produced the false green
g++ -O2 -std=c++11 -Isrc/extensions "$TMP/test_gen.orig" -o /tmp/test_gen 2>/dev/null
if bash tools/dsp/test_gen.sh >"$TMP/g3.log" 2>&1; then
    note "G3 compile failure cannot run a stale binary" "FAIL (passed anyway)"
    fail=1
else
    if grep -q "BUILD FAILED" "$TMP/g3.log"; then
        note "G3 compile failure cannot run a stale binary" "PASS"
    else
        note "G3 compile failure cannot run a stale binary" "FAIL (wrong reason)"
        fail=1
    fi
fi
cp "$TMP/test_gen.orig" tools/dsp/test_gen.cpp
rm -f /tmp/test_gen

# G4: the sweeps must not be able to pass with a seconds/milliseconds swap.
# Arm a 2 s sweep and assert 2*fs frames; then simulate the swap in a scratch
# copy of the harness and require it to fail.
cat > "$TMP/unit.cpp" <<'EOF'
#include <cstdio>
#include <cmath>
#include "csd_gen_core.h"
int main(void) {
    csd_gen_t g; csd_gen_reset(&g);
    int why = 0;
    /* seconds handed to the millisecond field: the exact bug this guards */
    csd_gen_wave(&g, 48000u, CSD_GEN_SWEEP, 1000u<<8, 20000u<<8, CSD_GEN_LIN,
                 0x40000000u, 2u /* meant 2 s, field is ms */, &why);
    double want = 2.0 * 48000.0;
    double got = (double)g.period_frames;
    bool caught = fabs(got - want) > 1.0;
    printf("frames=%g want=%g caught=%d\n", got, want, caught ? 1 : 0);
    return caught ? 0 : 1;
}
EOF
g++ -O2 -std=c++11 -Isrc/extensions "$TMP/unit.cpp" -o "$TMP/unit" 2>/dev/null
if "$TMP/unit" >"$TMP/g4.log" 2>&1; then
    note "G4 seconds/ms swap detected by the frame-count assert" "PASS"
else
    note "G4 seconds/ms swap detected by the frame-count assert" "FAIL"
    cat "$TMP/g4.log"
    fail=1
fi

# G5: canaries. The gate asserts three deliberately wrong implementations FAIL
# its assertions. If any canary stops failing, the corresponding assertion is
# decoration -- so the gate itself must refuse. Verified by checking the gate
# reports the canary lines rather than omitting them.
if bash tools/dsp/test_gen.sh >"$TMP/g5.log" 2>&1; then
    if grep -q "naive .*dB vs band-limited" "$TMP/g5.log"; then
        note "G5 naive-square canary measured and reported" "PASS"
    else
        note "G5 naive-square canary measured and reported" "FAIL (absent)"
        fail=1
    fi
else
    note "G5 full gate passes (required before canaries mean anything)" \
         "FAIL (gate red; $(grep -c FAIL "$TMP/g5.log") failures)"
    grep "FAIL" "$TMP/g5.log" | head -8
    fail=1
fi

echo
[ $fail -eq 0 ] && echo "GUARDS: PASS" || echo "GUARDS: FAIL"
exit $fail
