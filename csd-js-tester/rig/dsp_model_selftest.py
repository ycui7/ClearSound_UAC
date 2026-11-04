#!/usr/bin/env python3
"""Gate for rig/dsp_bit_perfect.py BEFORE any device time is spent.

The rig's expectation is only trustworthy if its host port of adsp_biquad is
bit-identical to the vendor's own VPU model (audio_dsp.dsp.biquad.
biquad.process_xcore, the model lib_audio_dsp's test suite validates against
the silicon). Two things are checked:

  1. biquad_stream(rounding="vpu") == process_xcore, on random int32 streams,
     all eight rate tables. Any single-bit difference is a FAIL: the rig would
     then compare the device against a stream the device never runs.
  2. biquad_stream(rounding="once") != process_xcore on at least some inputs
     (if they were identical the A/B in the rig would be vacuous and one of
     the two "suspects" would be imaginary).

Also checks the wire-scaling helper and the DoP scanner against hand-built
edge cases, because those are the two places a silent wrong answer turns a
PASS into a lie.
"""
import os
import sys
import numpy as np

sys.path.insert(0, ".")
from dsp_bit_perfect import biquad_stream, BQ, dop_would_latch
import audio_dsp.dsp.biquad as xbq

rng = np.random.default_rng(20261104)
fails = 0


def check(name, cond, detail=""):
    global fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name} {detail}")
    if not cond:
        fails += 1


print("== 1. port vs vendor model (the gate) ==")
for rate, (coeffs, lsh) in BQ.items():
    # float coeffs: the ints ARE coeffs * 2**(30-lsh), recover exactly
    fl = [c / float(1 << (30 - lsh)) for c in coeffs]
    f = xbq.biquad(fl, fs=rate)
    x = [int(rng.integers(-(1 << 31), 1 << 31)) for _ in range(2000)]
    # exercise the extremes too: full-scale pairs and zeros (state warm-up)
    x += [2147483647, -2147483648, 0, 0, 0, 2147483647, -2147483648]
    mine = biquad_stream(x, coeffs, lsh, "vpu")
    theirs = [int(f.process_xcore(v, channel=0)) for v in x]
    same = mine == theirs
    n_bad = sum(1 for m, t in zip(mine, theirs) if m != t)
    first = next((i for i, (m, t) in enumerate(zip(mine, theirs)) if m != t), None)
    check(f"rate {rate} vpu == process_xcore", same,
          "" if same else f"({n_bad} differ, first @{first}: "
                          f"mine={mine[first]} theirs={theirs[first]})")

print("== 1b. the C twin == vendor model (the binary the rig actually runs) ==")
# The rig filters 23M frames with rig/dsp_bq_host (C), not the python loop
# above -- a python-only proof would leave the executed binary unverified.
import subprocess, tempfile
exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dsp_bq_host")
if not os.path.exists(exe):
    check("C twin built", False, f"(missing {exe}: gcc -O2 -o dsp_bq_host dsp_bq_host.c)")
else:
    rate = 384000
    coeffs, lsh = BQ[rate]
    fl = [c / float(1 << (30 - lsh)) for c in coeffs]
    f = xbq.biquad(fl, fs=rate)
    x = [int(rng.integers(-(1 << 31), 1 << 31)) for _ in range(20000)]
    x += [2147483647, -2147483648, 0, 0, 2147483647, -2147483648]
    import numpy as _np2
    with tempfile.TemporaryDirectory() as td:
        inf, outf = td + "/in.bin", td + "/out.bin"
        _np2.array([[v, v] for v in x], dtype=_np2.int32).tofile(inf)
        r = subprocess.run([exe, inf, outf] + [str(c) for c in coeffs] + [str(lsh)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            check("C twin ran", False, f"(rc={r.returncode} {r.stderr.strip()})")
        else:
            got = _np2.fromfile(outf, dtype=_np2.int32).reshape(-1, 2)[:, 0].tolist()
            theirs = [int(f.process_xcore(v, channel=0)) for v in x]
            same = got == theirs
            n_bad = sum(1 for m, t in zip(got, theirs) if m != t)
            first = next((i for i, (m, t) in enumerate(zip(got, theirs)) if m != t), None)
            check("C twin == process_xcore (384k, 20k samples + extremes)", same,
                  "" if same else f"({n_bad} differ, first @{first})")

print("== 2. the A/B is real: 'once' must differ from the model ==")
rate = 384000
coeffs, lsh = BQ[rate]
fl = [c / float(1 << (30 - lsh)) for c in coeffs]
f = xbq.biquad(fl, fs=rate)
x = [int(rng.integers(-(1 << 31), 1 << 31)) for _ in range(4000)]
once = biquad_stream(x, coeffs, lsh, "once")
theirs = [int(f.process_xcore(v, channel=0)) for v in x]
n_diff = sum(1 for m, t in zip(once, theirs) if m != t)
check("'once' differs from model somewhere", n_diff > 0, f"({n_diff}/4000 differ)")

print("== 3. DoP scanner (on DEVICE words: v24<<8, as csd_dsp sees them) ==")
# DOP_MARK tests byte0 and byte2 of the WORD (csd_dsp.xc:93). A real DoP
# frame carries the marker in byte0 and byte2 of the word the DSP receives.
FA, FIVE = 0xFA, 0x05
def devword(b2, b1, b0):
    return ((b2 & 0xFF) << 16) | ((b1 & 0xFF) << 8) | (b0 & 0xFF)
dopL = [devword(FIVE, 0x33, FIVE) for _ in range(4)]
dopR = [devword(FA, 0x77, FA) for _ in range(4)]
noise = [devword(0x12, 0x34, 0x56)] * 100
check("latches after 4 frames marked on BOTH channels",
      dop_would_latch(noise + dopL + noise, noise + dopR + noise) == 100,
      f"(got {dop_would_latch(noise + dopL + noise, noise + dopR + noise)})")
check("3 marked frames never latch",
      dop_would_latch(noise + dopL[:3] + noise, noise + dopR[:3] + noise) is None)
check("L marked, R unmarked never latches",
      dop_would_latch(noise + dopL + noise, noise + [0x9ABCDE] * 4 + noise) is None)
check("byte0 != byte2 never latches",
      dop_would_latch(noise + [devword(FIVE, 0, 0) for _ in range(8)] + noise,
                      noise + [devword(FIVE, 0, 0) for _ in range(8)] + noise) is None)
# The property that makes THIS rig safe by construction: the wire codec puts
# the 24-bit value in bits 31..8, so byte0 == 0 always -> structurally unable
# to trip the latch. If this ever stops holding, the wire format changed.
from bit_perfect_noise import pack_frames, unpack_frames
import csd_signal as sig
idx = np.arange(200000, dtype=np.uint64)
sL, sR = sig.expected_vec(idx)
w = pack_frames(sL, sR)
w32 = np.frombuffer(w, dtype="<u4")
check("rig stimulus can never DoP-latch (byte0 always 0)",
      int((w32 & 0xFF).max()) == 0, f"(max byte0={int((w32 & 0xFF).max())})")

print("== 3b. anchor: find_delta/build_regions against a KNOWN offset ==")
# The marker-settling idea died here: poles at |z| = 0x3FC00000/2**30 =
# 0.99866 give a ~16,000-sample decay, longer than the rig's 4096-frame
# marker, so a FILTERED marker never becomes constant and the aligner's
# sync-run anchoring cannot be used at a filtering preset. Replaced by an
# offset search. This test replaces the dead assumption: a rig that cannot
# recover a KNOWN offset on synthetic data cannot certify hardware.
from dsp_bit_perfect import find_delta, build_regions, filtered_expectation
import numpy as _np
import one_shot_block as O
N, SHIFT = 60000, 137                    # deliberately odd offset
_bL, _bR, _raw = O.build_block(N + SHIFT)
uL = _bL.astype(_np.int64); uR = _bR.astype(_np.int64)
_eL, _eR = filtered_expectation(uL, uR, 384000, "vpu")
fuL = _np.array([v & 0xFFFFFF for v in _eL], dtype=_np.int64)
fuR = _np.array([v & 0xFFFFFF for v in _eR], dtype=_np.int64)
cL = fuL[SHIFT:]; cR = fuR[SHIFT:]       # what the tap returns, no gap
frac, delta = find_delta(cL, cR, fuL, fuR)
check("find_delta recovers the known offset",
      delta == SHIFT and frac == 8192, f"(got delta={delta} frac={frac})")
d_ok = delta if delta is not None else SHIFT
regs = build_regions(cL, cR, fuL, fuR, d_ok)
cov = sum(b - x for (x, b, _) in regs)
check("build_regions covers a clean capture", cov >= 0.9 * len(cL),
      f"({cov:,}/{len(cL):,})")
HOLE, HAT = 20000, 48                    # exactly one ISO packet at 384 kHz
cL2 = cL.copy(); cR2 = cR.copy()
cL2[HOLE:HOLE + HAT] = -1; cR2[HOLE:HOLE + HAT] = -1
regs2 = build_regions(cL2, cR2, fuL, fuR, d_ok)
inside = any(x <= HOLE + 20 < b for (x, b, _) in regs2)
check("a 48-frame drop is EXCLUDED, not scored clean", not inside)
kept = sum(b - x for (x, b, _) in regs2)
check("the drop does not void the whole run", kept >= 0.8 * len(cL),
      f"({kept:,} kept)")
frac_bad, _ = find_delta(cL, cR, fuL ^ 1, fuR ^ 1)
check("a corrupted expectation anchors LOUD", frac_bad < 8192,
      f"(best {frac_bad}/8192)")
print("== 4. END-TO-END synthetic: the rig's own pipeline, known answer ==")
# Every bug this rig has had so far was host-side and needed no hardware:
# the >>8-vs-mask direction, the delta sign, an int32/int64 ABI mismatch, and
# a numpy cast that only failed past 60k frames. This section runs the rig's
# ACTUAL chain -- schedule -> wire word -> C twin (the device's math) -> tap
# word -> unpack -> find_delta -> build_regions -> align_score.score -- and
# requires PASS on a clean case and FAIL on an injected one. A rig that cannot
# grade a synthetic case it already knows the answer to cannot certify the
# device, and this is the check that replaces four wasted hardware launches.
from dsp_bit_perfect import (find_delta, build_regions, filtered_expectation)
import align_score as A
import one_shot_block as O
import numpy as _np3
from bit_perfect_noise import pack_frames, unpack_frames

N5, SHIFT5 = 120000, 911
bL, bR, _raw = O.build_block(N5 + SHIFT5)          # 24-bit fields
eL5, eR5 = filtered_expectation(bL.astype(_np3.int32), bR.astype(_np3.int32),
                                384000, "vpu")
# what the tap records is the FILTERED wire word; what the reader yields is
# that word >> 8 (bit_perfect_noise.unpack_frames). Build both the same way
# the hardware would, from the schedule alone.
uL5 = (eL5.astype(_np3.uint32) >> _np3.uint32(8)).astype(_np3.int32)
uR5 = (eR5.astype(_np3.uint32) >> _np3.uint32(8)).astype(_np3.int32)
capL = uL5[SHIFT5:].copy()
capR = uR5[SHIFT5:].copy()

f5, d5 = find_delta(capL, capR, uL5, uR5)
check("e2e: anchor recovers the offset", d5 == SHIFT5 and f5 == 8192,
      f"(delta={d5} frac={f5})")
regs5 = build_regions(capL, capR, uL5, uR5, d5 if d5 is not None else SHIFT5)
sc5 = A.score(capL, capR, O.sent_lookup(uL5, uR5), regs5)
check("e2e clean case scores ZERO defects (a PASS is reachable)",
      sc5["defects"] == 0 and sc5["checked"] > 0.9 * len(capL),
      f"(defects={sc5['defects']} checked={sc5['checked']:,}/{len(capL):,})")

# negative offset: the proven rig measured delta = -236 on this DAC, and an
# earlier find_delta refused d < 0. Both signs must work or a good hardware
# run fails for the wrong reason.
SHIFTN = -300
# capture frame i corresponds to schedule frame i + delta, so for delta < 0
# the FIRST -delta capture frames map before the block and are unprovable:
# fill them with a value the schedule cannot contain (-1, which unpack_frames
# never returns) so anchoring must skip them rather than match by luck.
capLn = _np3.empty(N5, dtype=_np3.int32)
capRn = _np3.empty(N5, dtype=_np3.int32)
capLn[: -SHIFTN] = -1; capRn[: -SHIFTN] = -1
capLn[-SHIFTN:] = uL5[: N5 + SHIFTN]
capRn[-SHIFTN:] = uR5[: N5 + SHIFTN]
fN, dN = find_delta(capLn, capRn, uL5, uR5)
check("e2e: negative offset recovered", dN == SHIFTN and fN == 8192,
      f"(delta={dN} frac={fN})")
regsN = build_regions(capLn, capRn, uL5, uR5, dN if dN is not None else SHIFTN)
scN = A.score(capLn, capRn, O.sent_lookup(uL5, uR5), regsN)
check("e2e: negative offset scores clean", scN["defects"] == 0
      and scN["checked"] > 0.9 * len(capLn),
      f"(defects={scN['defects']} checked={scN['checked']:,})")

# inject: one bit in one frame -- the rig must see exactly one defect
capL2, capR2 = capL.copy(), capR.copy()
capL2[50000] ^= 1
sc5b = A.score(capL2, capR2, O.sent_lookup(uL5, uR5), regs5)
check("e2e: a flipped bit is reported (the rig CAN fail)",
      sc5b["defects"] == 1, f"(defects={sc5b['defects']})")

# inject: the WRONG rounding semantics (the plausible-but-wrong port) must not
# sneak through as a clean run
eL_bad, eR_bad = filtered_expectation(bL.astype(_np3.int32),
                                      bR.astype(_np3.int32), 384000, "vpu")
from dsp_bit_perfect import biquad_stream, BQ as _BQ
_c, _l = _BQ[384000]
_wire = (bL.astype(_np3.int32).astype(_np3.uint32) << _np3.uint32(8)).astype(_np3.int32)
_once = _np3.array(biquad_stream(_wire.tolist(), _c, _l, "once"), dtype=_np3.int32)
uL_bad = (_once.astype(_np3.uint32) >> _np3.uint32(8)).astype(_np3.int32)
f_bad, d_bad = find_delta(capL, capR, uL_bad, uR5)
check("e2e: a wrong-port expectation anchors LOW, never clean",
      f_bad < 8192, f"(best {f_bad}/8192)")

print("== 5. self-consistency: zero input stays zero ==")
z = biquad_stream([0] * 64, BQ[384000][0], 0, "vpu")
check("zero in -> zero out", all(v == 0 for v in z))

print()
print("ALL PASS -- rig expectation is vendor-model bit-exact"
      if fails == 0 else f"{fails} FAILED -- do NOT run against hardware")
sys.exit(1 if fails else 0)
