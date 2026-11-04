#!/usr/bin/env python3
"""Per-channel alignment with ROBUST anchors.

The earlier per-channel test reported "-1/8192 at delta=0" for every case --
which is not "no match", it is "no candidate was ever tried": it anchored on
capL[0] and that value does not occur in the first 2.5M schedule entries, so
the loop body never ran. A probe that tries nothing and prints "nothing
matched" is how a rig wastes a hardware run, so anchor properly:

  * build a value -> positions index over the WHOLE schedule (once),
  * try the first 64 kept frames as anchors, and a mid-stream window too,
  * score each candidate offset over an 8192-frame window, both channels,
    with L and R allowed DIFFERENT offsets (an I2S word-select skew or a
    one-slot tap/reader mismatch does exactly that).
"""
import numpy as np
import sys
from collections import defaultdict

WIN = 8192
NANCHOR = 64

cap = np.load("/tmp/dsp_cap.npy")
cL, cR = cap[0].astype(np.uint32), cap[1].astype(np.uint32)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
fL = (exp[0].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
fR = (exp[1].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
del exp
print(f"schedule {len(fL):,}; tap words are Q31 (byte0 not forced to 0), "
      f"rig compares the top 24 bits")

real = ~((cL == 0) & (cR == 0))
idx = np.flatnonzero(real)
kL, kR = cL[real], cR[real]
del cL, cR
print(f"kept {len(kL):,} frames")

def index_of(vals, name):
    d = defaultdict(list)
    for pos, v in enumerate(vals):
        d[int(v)].append(pos)
    return d

# index only what we need: positions of the anchor values, via searchsorted
def positions(sch, v, cap_n=4000):
    hit = np.flatnonzero(sch == v)
    return hit[:cap_n]

def score(cap_seg, sch, d):
    if d < 0 or d + len(cap_seg) > len(sch):
        return -1
    return int(np.count_nonzero(cap_seg == sch[d:d + len(cap_seg)]))

def best_for(cap_seg, sch, tag):
    """Try every anchor value from the first NANCHOR capture frames."""
    best = (-1, 0, -1)
    for a in range(min(NANCHOR, len(cap_seg))):
        v = int(cap_seg[a])
        for c in positions(sch, v):
            d = int(c) - a
            m = score(cap_seg, sch, d)
            if m > best[0]:
                best = (m, d, a)
    m, d, a = best
    pct = 100.0 * m / max(1, len(cap_seg))
    print(f"  {tag:44s}: {m:,}/{len(cap_seg):,} = {pct:6.2f}% "
          f"at delta={d:,} (anchor #{a})")
    return m, d

for wname, ws in (("start", 0), ("mid", len(kL) // 2), ("late", int(len(kL) * 0.9))):
    segL = kL[ws:ws + WIN]
    segR = kR[ws:ws + WIN]
    if len(segL) < WIN:
        continue
    print(f"\nwindow {wname} (kept {ws:,}):")
    mL, dL = best_for(segL, fL, "capL vs filtered L")
    mR, dR = best_for(segR, fR, "capR vs filtered R")
    xL, dxL = best_for(segL, fR, "capL vs filtered R (crosstalk)")
    xR, dxR = best_for(segR, fL, "capR vs filtered L (crosstalk)")
    if mL > 0.99 * WIN and mR > 0.99 * WIN:
        print(f"  ==> BOTH channels bit-exact at their own offsets "
              f"(dL={dL:,}, dR={dR:,}, skew={dR-dL:+,}). The DSP arithmetic "
              "is bit-perfect; the L/R pairing is what is broken.")
    elif max(mL, mR) > 0.99 * WIN:
        print("  ==> one channel bit-exact, the other not: asymmetric defect.")
    if mL + mR + xL + xR == -4:
        print("  ==> no candidate tried in this window either")
