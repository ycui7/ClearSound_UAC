#!/bin/bash
# Prove the harness guards fire. Each check breaks something on purpose and
# requires the harness to NOTICE. A guard that has never been observed to fail
# is not a guard -- this session produced a stale-binary false green, a
# round-trip assert that reduced to `x - x == 0`, and a find-based staleness
# check that silently matched nothing. All three looked like working checks.
#
#   usage: tools/dsp/test_guards.sh
#
# NOTHING HERE TOUCHES THE WORKING TREE.
#
# An earlier revision edited src/extensions/*.h in place, restored them with a
# `cp` at the end of each block, and cleaned $TMP in an EXIT trap. Two ways that
# failed. (1) A run interrupted between the edit and the restore left a
# CORRUPTED GENERATED TABLE in the working tree -- and because the backup lived
# in $TMP, the trap deleted the only copy that could undo it. One `git add .`
# from committed. (2) Two concurrent runs restored over each other: run A's
# restore landed between run B's edit and B's check, so B compared a pristine
# file and reported "gate passed" for a mutation that had already been undone.
# That produced a false FAIL whose message blamed the guard.
#
# Both disappear when the mutations happen in a throwaway copy of the tree. The
# real files are only ever read, so an interruption costs the sandbox and
# nothing else, and concurrent runs cannot see each other's damage.
#
# Snapshot git state up front: G7 compares against THIS, so a file the user
# already had modified cannot make the harness look guilty.
set -u
APP=$HOME/sw-audio-sc/sw_usb_audio/clearsound_uac
cd "$APP" || exit 1
fail=0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Sandbox: just the files the host gate reads. `cp -a` preserves mtimes, which
# matters -- test_gen.sh refuses when a source is newer than the binary it just
# built, and a copy that rewrote mtimes would trip that check for the wrong
# reason.
SB=$TMP/app
mkdir -p "$SB/src/extensions" "$SB/tools/dsp" || exit 1
cp -a src/extensions/csd_gen_core.h   "$SB/src/extensions/" || exit 1
cp -a src/extensions/csd_gen_tables.h "$SB/src/extensions/" || exit 1
cp -a tools/dsp/test_gen.cpp          "$SB/tools/dsp/"      || exit 1
cp -a tools/dsp/test_gen.sh           "$SB/tools/dsp/"      || exit 1
cp -a tools/dsp/gen_gen_tables.py     "$SB/tools/dsp/"      || exit 1
SB_CORE=$SB/src/extensions/csd_gen_core.h
SB_TABLES=$SB/src/extensions/csd_gen_tables.h
SB_MAIN=$SB/tools/dsp/test_gen.cpp

# Baseline for G7. Taken here, before any mutation, so a file the user already
# had modified is not charged to the harness.
git_before=$(cd "$APP" && git status --porcelain -- src/extensions tools/dsp \
             src/core 2>/dev/null)

# Run the gate against the sandbox. APP is an override in test_gen.sh precisely
# so this is possible; the gate still does its own T0 reproduce-the-tables
# check, so what is verified here is the real gate, not a stub of it.
run_gate() { APP="$SB" bash "$SB/tools/dsp/test_gen.sh" >"$1" 2>&1; }

note() { printf '%-58s %s\n' "$1" "$2"; }

# A mutation must actually mutate. Every guard below that breaks a file asserts
# this first: a check whose setup silently did nothing reports "guard fired"
# for a file that was never broken. An earlier revision used
# `sed '0,/pat/! s/pat/.../'`, whose negated 0-range EXCLUDES the first matching
# line -- the targeted line -- so it changed nothing and G2 passed vacuously.
#
#   $1 file   $2 anchor (must exist)   $3 replacement (count=1)
break_file() {
    python3 - "$1" "$2" "$3" <<'EOF'
import sys
p, anchor, repl = sys.argv[1:4]
s = open(p).read()
# count=1, not "occurs exactly once": a sine table is symmetric
# (sin(2*pi*k/N) == sin(2*pi*(N-k)/N)), so every entry appears twice by
# construction and uniqueness is the wrong requirement. What has to be true is
# that the anchor is present and the file ends up different -- and the cmp in
# mutated() is the authority on that, not this script's opinion.
if s.count(anchor) < 1:
    raise SystemExit("anchor %r absent -- mutation aborted" % anchor)
open(p, "w").write(s.replace(anchor, repl, 1))
EOF
}

# ---------------------------------------------------------------------------
# G1: the generator must refuse when the core and the generator disagree on the
# square's harmonic count (it sizes the Gibbs table in one and bounds the
# oscillator loop in the other). A silent agreement here would emit a table the
# core indexes out of.
cp -a "$SB_CORE" "$TMP/core.orig"
break_file "$SB_CORE" "#define CSD_GEN_SQ_HARMONICS 32" \
                    "#define CSD_GEN_SQ_HARMONICS 24"
if cmp -s "$SB_CORE" "$TMP/core.orig"; then
    note "G1 setup: core actually modified" "FAIL (edit changed nothing)"
    fail=1
fi
# The output goes INSIDE the sandbox so the generator's own resolution order
# (core next to the output, then CWD-relative src/extensions) can only ever find
# the mutated core. Writing it to $TMP instead would let both lookups miss, and
# a "core not found" refusal would satisfy a grep for REFUSED -- the guard
# passing while testing nothing, the exact class of bug this file exists to
# catch. The message check below names the disagreement for the same reason.
if python3 tools/dsp/gen_gen_tables.py "$SB/src/extensions/out.h" \
        >"$TMP/g1.log" 2>&1; then
    note "G1 harmonic-count disagreement refused" "FAIL (accepted)"
    fail=1
else
    if grep -q "TABLE GENERATOR REFUSED" "$TMP/g1.log" \
       && grep -qi "HARMONICS" "$TMP/g1.log"; then
        note "G1 harmonic-count disagreement refused" "PASS"
    else
        note "G1 harmonic-count disagreement refused" "FAIL (wrong reason)"
        sed -n '1,3p' "$TMP/g1.log"
        fail=1
    fi
fi
rm -f "$SB/src/extensions/out.h"
# No restore of the core here on purpose; G2 restores it, because G5 and G6 run
# the full gate and would otherwise run it sabotaged. Either way the working
# tree is safe: this file is only mutated inside $SB, so the difference between
# restoring and not restoring is which guard reports the problem, not whether
# the repo can end up corrupted -- which was the old design's actual exposure.

# ---------------------------------------------------------------------------
# G2: a hand-edited generated table must be caught by the byte-identity check in
# test_gen.sh (T0), not silently compiled.
#
# The order matters: restore the core FIRST so the only defect left is the
# hand-edited table. If the core were still sabotaged, T0 would fail for the
# wrong reason and the guard would pass while testing nothing -- which is why
# "wrong reason" below is a distinct, failing outcome and not a warning.
cp -a "$TMP/core.orig" "$SB_CORE"
break_file "$SB_TABLES" "13176712" "13176713"
if cmp -s "$APP/src/extensions/csd_gen_tables.h" "$SB_TABLES"; then
    note "G2 setup: table actually modified" "FAIL (edit changed nothing)"
    fail=1
else
    note "G2 setup: table actually modified" "PASS"
fi
if run_gate "$TMP/g2.log"; then
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
cp -a "$APP/src/extensions/csd_gen_tables.h" "$SB_TABLES"

# ---------------------------------------------------------------------------
# G3: a failed compile must not run anything.
#
# The original version of this harness piped g++ through `head`, which exits 0,
# so `g++ ... | head && ./test_gen` executed the PREVIOUS binary after a failed
# compile and printed its verdict as the new one. The gate now builds to
# /tmp/test_gen.$$ -- a name no earlier run could have left behind -- so the
# stale binary can no longer be found even by accident. This guard checks the
# outcome rather than the mechanism: it must fail, say BUILD FAILED, and above
# all must not print gate results, which is the positive proof nothing executed.
# A decoy is planted at the legacy fixed path to show the gate no longer depends
# on it.
cp -a "$SB_MAIN" "$TMP/test_gen.orig"
g++ -O2 -std=c++11 -I"$SB/src/extensions" "$TMP/test_gen.orig" -o /tmp/test_gen \
    2>/dev/null
printf '\nthis is not valid c++\n' >> "$SB_MAIN"
if run_gate "$TMP/g3.log"; then
    note "G3 compile failure cannot run a stale binary" "FAIL (passed anyway)"
    fail=1
elif ! grep -q "BUILD FAILED" "$TMP/g3.log"; then
    note "G3 compile failure cannot run a stale binary" "FAIL (wrong reason)"
    tail -3 "$TMP/g3.log"
    fail=1
elif grep -qE "checks, [0-9]+ failures" "$TMP/g3.log"; then
    # The signature of the original bug: a gate verdict after a failed compile
    # can only have come from a binary that was not built from this source.
    note "G3 compile failure cannot run a stale binary" \
         "FAIL (printed gate results after a failed compile)"
    grep -E "checks," "$TMP/g3.log" | head -2
    fail=1
else
    note "G3 compile failure cannot run a stale binary" "PASS"
fi
cp -a "$TMP/test_gen.orig" "$SB_MAIN"
rm -f /tmp/test_gen

# ---------------------------------------------------------------------------
# G4: the sweeps must not be able to pass with a seconds/milliseconds swap.
# Arm a 2 s sweep and assert 2*fs frames; the unit asserts the mismatch is
# CAUGHT, so it fails if the API ever starts accepting seconds.
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
g++ -O2 -std=c++11 -I"$SB/src/extensions" "$TMP/unit.cpp" -o "$TMP/unit" \
    2>"$TMP/g4.build"
if [ ! -x "$TMP/unit" ]; then
    note "G4 seconds/ms swap detected by the frame-count assert" \
         "FAIL (unit did not compile)"
    sed -n '1,6p' "$TMP/g4.build"
    fail=1
elif "$TMP/unit" >"$TMP/g4.log" 2>&1; then
    note "G4 seconds/ms swap detected by the frame-count assert" "PASS"
else
    note "G4 seconds/ms swap detected by the frame-count assert" "FAIL"
    cat "$TMP/g4.log"
    fail=1
fi

# ---------------------------------------------------------------------------
# G5: canaries. The gate asserts three deliberately wrong implementations FAIL
# its assertions. If any canary stops failing, the corresponding assertion is
# decoration -- so the gate itself must refuse. Verified by checking the gate
# reports the canary lines rather than omitting them.
if run_gate "$TMP/g5.log"; then
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

# ---------------------------------------------------------------------------
# G6: concurrent runs must not interfere.
#
# Regression guard for the race that produced a false failure in this session.
# The FIRST version of this guard was itself a false guard: it ran two passing
# builds and required both to say PASS -- but a passing build never READS the
# shared build log (it prints it only when non-empty), so reverting BUILD_LOG to
# the shared /tmp/gen_build.log left this guard green. Verified: it fired not at
# all. A guard must be exercised on the path where the resource is used.
#
# So each concurrent run builds from its own sandbox with a UNIQUELY-NAMED
# unused variable, which makes the compiler emit a token into that run's build
# log, and the gate prints warnings when the log is non-empty. Each run must see
# its OWN token and not the other's. PLUS a deterministic existence check: the
# shared path must not be created at all. (The token check alone is not enough:
# each g++ opens its output with O_TRUNC at start and writes once at exit, so
# two same-sized warnings overwrite cleanly and a reader usually still sees only
# its own -- timing luck, not safety. The existence check fails a reverted gate
# every time, which is what makes this guard mean something.)
rm -f /tmp/gen_build.log        # scratch namespace; start from absent
make_sandbox() {
    local sb=$1 token=$2
    mkdir -p "$sb/src/extensions" "$sb/tools/dsp" || return 1
    cp -a "$APP/src/extensions/csd_gen_core.h"   "$sb/src/extensions/" || return 1
    cp -a "$APP/src/extensions/csd_gen_tables.h" "$sb/src/extensions/" || return 1
    cp -a "$APP/tools/dsp/test_gen.sh"           "$sb/tools/dsp/"      || return 1
    cp -a "$APP/tools/dsp/gen_gen_tables.py"     "$sb/tools/dsp/"      || return 1
    # The warning is appended, not substituted: everything else stays identical
    # to the real build, so the only variable is which run owns which log.
    #
    # #warning, not an unused variable: this build is -Wall -Wextra and still
    # emits ZERO warnings, so an unused-variable token would never reach the log
    # and the check would fail for reasons unrelated to concurrency -- the same
    # trap as the guard itself. #warning guarantees the token is in the log.
    {   cp -a "$APP/tools/dsp/test_gen.cpp" "$sb/tools/dsp/" || return 1
        printf '\n#warning csd_g6_marker_%s\n' "$token" \
               >> "$sb/tools/dsp/test_gen.cpp"
    }
    return 0
}

SBA=$TMP/g6a; SBB=$TMP/g6b
make_sandbox "$SBA" "alpha" || { note "G6 sandbox build" "FAIL (setup)"; fail=1; }
make_sandbox "$SBB" "bravo" || { note "G6 sandbox build" "FAIL (setup)"; fail=1; }
if [ -d "$SBA" ] && [ -d "$SBB" ]; then
    APP="$SBA" bash "$SBA/tools/dsp/test_gen.sh" >"$TMP/g6a.log" 2>&1 &
    pida=$!
    APP="$SBB" bash "$SBB/tools/dsp/test_gen.sh" >"$TMP/g6b.log" 2>&1 &
    pidb=$!
    rc_a=0; rc_b=0
    wait "$pida" || rc_a=$?
    wait "$pidb" || rc_b=$?
    g6_bad=""
    [ $rc_a -eq 0 ] && [ $rc_b -eq 0 ] || g6_bad="rc_a=$rc_a rc_b=$rc_b"
    grep -q "GEN HOST GATE: PASS" "$TMP/g6a.log" || g6_bad="$g6_bad a:not-pass"
    grep -q "GEN HOST GATE: PASS" "$TMP/g6b.log" || g6_bad="$g6_bad b:not-pass"
    [ "$(grep -c 'checks, 0 failures' "$TMP/g6a.log")" = "1" ] \
        || g6_bad="$g6_bad a:verdict-count"
    [ "$(grep -c 'checks, 0 failures' "$TMP/g6b.log")" = "1" ] \
        || g6_bad="$g6_bad b:verdict-count"
    # Own token present, other's absent, in the WARNING SECTION each run prints.
    grep -q "csd_g6_marker_alpha" "$TMP/g6a.log" \
        || g6_bad="$g6_bad a:lost-own-warning"
    grep -q "csd_g6_marker_bravo" "$TMP/g6b.log" \
        || g6_bad="$g6_bad b:lost-own-warning"
    grep -q "csd_g6_marker_bravo" "$TMP/g6a.log" \
        && g6_bad="$g6_bad a:saw-b-warnings"
    grep -q "csd_g6_marker_alpha" "$TMP/g6b.log" \
        && g6_bad="$g6_bad b:saw-a-warnings"
    # Deterministic half: a per-run gate must never create the shared path.
    # This is the assertion that actually fails on a reverted gate, unlike the
    # token check above, which is timing luck.
    if [ -e /tmp/gen_build.log ]; then
        g6_bad="$g6_bad shared-build-log-created"
        rm -f /tmp/gen_build.log
    fi
    if [ -z "$g6_bad" ]; then
        note "G6 two concurrent gate runs, warnings uncrossed" "PASS"
    else
        note "G6 two concurrent gate runs, warnings uncrossed" \
             "FAIL ($g6_bad)"
        fail=1
    fi
fi

# ---------------------------------------------------------------------------
# G7: the working tree must be exactly as it was found.
#
# Not decoration: the previous design's failure mode was a mutated tree, and
# this is the assertion that would have caught it. Checked against git rather
# than a snapshot, so it also catches a stray file the harness created.
after=$(cd "$APP" && git status --porcelain -- src/extensions tools/dsp \
         src/core 2>/dev/null)
if [ "$after" = "$git_before" ]; then
    note "G7 working tree untouched by the harness" "PASS"
else
    note "G7 working tree untouched by the harness" "FAIL (tree modified)"
    diff <(printf '%s\n' "$git_before") <(printf '%s\n' "$after") | head -8
    fail=1
fi

echo
[ $fail -eq 0 ] && echo "GUARDS: PASS" || echo "GUARDS: FAIL"
exit $fail
