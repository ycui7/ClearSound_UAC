#!/usr/bin/env python3
"""Dropout-tolerant DSP comparison on the SAVED capture. No device.

The capture is our stream with 96 zero frames punched out of every 672
(zero_pattern.py: 34,688 runs, all exactly 96, spacing 672, both channels
together). Plain cross-correlation cannot align that -- after the first hole,
capture index i no longer names schedule index i+delta.

Use the MEASURED hole positions rather than an assumed model. Two index maps
for the kept frames:

  M1  s = capture_index + delta   -- the device frame clock kept ticking
      through the zeros and the TX stream advanced with it, so the zeros are
      silence on the ADC leg only
  M2  s = kept_index    + delta   -- the exchange skipped those frames, so the
      TX read pointer did NOT advance: kept capture is contiguous in schedule

Whichever map aligns says who owns the dropout. If the match is then complete,
the DSP arithmetic is bit-perfect on everything delivered and the dropout is a
separate, already-characterized delivery defect.
"""
import numpy as np
import sys

WIN = 65536
PERIOD = 672

cap = np.load("/tmp/dsp_cap.npy")
cL, cR = cap[0].astype(np.uint32), cap[1].astype(np.uint32)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
uL = (exp[0].astype(np.uint32) >> np.uint32(8)).astype(np.int32)
uR = (exp[1].astype(np.uint32) >> np.uint32(8)).astype(np.int32)
del exp

real = ~((cL == 0) & (cR == 0))
capidx = np.flatnonzero(real).astype(np.int64)     # position in the capture
capL = cL[real].astype(np.int32)
capR = cR[real].astype(np.int32)
print(f"capture {len(cL):,} frames -> {len(capL):,} kept "
      f"({100*len(capL)/len(cL):.2f}%)")


def probe(base, tag):
    """Best (matches, delta) aligning kept capture onto schedule via base[]."""
    n0 = min(WIN, len(capL))
    b0 = base[:n0]
    v = int(capL[0])
    cands = np.flatnonzero(uL[:2_500_000] == v)[:40]
    best = (-1, 0, 0)
    for c in cands:
        d = int(c) - int(b0[0])
        if d < 0:
            continue
        j = b0 + d
        if j[-1] >= len(uL):
            continue
        m = int(np.count_nonzero((capL[:n0] == uL[j]) &
                                 (capR[:n0] == uR[j])))
        if m > best[0]:
            best = (m, d, n0)
    m, d, n = best
    print(f"  {tag}: best {m:,}/{n:,} ({100.0*m/max(1,n):.3f}%) at delta={d}")
    return m, d, n, base


rM1 = probe(capidx, "M1 capture-index (device clock ran through)")
rM2 = probe(np.arange(len(capL), dtype=np.int64),
            "M2 kept-index    (TX skipped them)")
m, d, n, base = max((rM1, rM2), key=lambda r: r[0] / max(1, r[2]))
if m < 0.99 * n:
    print("\nNeither map aligns the kept frames: the capture is not this "
          "filtered stream under either dropout model. Not an alignment bug.")
    sys.exit(1)

print(f"\nMap {'M1' if base is rM1[3] else 'M2'} aligns at delta={d}. "
      f"Verifying all {len(capL):,} kept frames...")
j = base + d
ok = (j >= 0) & (j < len(uL))
j, aL, aR = j[ok], capL[ok], capR[ok]
bad = np.union1d(np.flatnonzero(aL != uL[j]), np.flatnonzero(aR != uR[j]))
print(f"compared {len(aL):,} frames: {len(bad):,} defective "
      f"({100.0*len(bad)/max(1,len(aL)):.6f}%)")
if len(bad) == 0:
    print("VERDICT: BIT-PERFECT on every frame the device delivered. The "
          "96/672 dropout is a separate delivery defect.")
else:
    k = int(bad[0])
    print(f"first bad kept frame {k:,} -> schedule {int(j[k]):,}")
    print(f"  capL=0x{int(aL[k]):06X} expL=0x{int(uL[j[k]]):06X}  "
          f"capR=0x{int(aR[k]):06X} expR=0x{int(uR[j[k]]):06X}")
    vals, cnts = np.unique(j[bad] % PERIOD, return_counts=True)
    o = np.argsort(-cnts)[:6]
    print("bad-frame phase within the 672 period:",
          ", ".join(f"{vals[t]}x{cnts[t]}" for t in o))
