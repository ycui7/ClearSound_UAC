#!/usr/bin/env python3
"""Decisive: distribution of (capture - twin) at the good delta.

Established, with controls:
  * rare-value positional test spikes ONLY against the FILTERED twin (17,342 at
    delta 146, chance ~0.28); the UNFILTERED control is flat (4 of 572,956), so
    the device really did filter, and my membership percentages were not lying;
  * L and R both spike at the SAME delta with near-equal counts (17,342/17,309)
    and both crosstalk pairings are flat -- correct pairing, constant delay;
  * every comparison BIT FIELD was tried: none carries more than 1.18% of the
    mass, so this is not a low-24/high-24 basis error;
  * RMS agrees to 1e-5 and every spectral band agrees to 0.00 dB.

That combination -- same spectrum, same RMS, correct constant delay, but only
1.18% of values exactly equal -- is the signature of a SMALL SYSTEMATIC
DIFFERENCE in the last stage: the twin is missing (or adding) something that
perturbs each sample by a few least-significant bits. Rounding, dither, a
second volume/trim multiply, or a saturation path would all do it.

The test is one histogram: for every frame, capture - twin at delta 146.
  concentrated at 0 with a few-LSB spread -> the twin is missing a late stage;
                                              find it and the test will close.
  huge / uniform                           -> the capture is unrelated noise with
                                              the same spectrum; the spike was
                                              an artifact and the delivery path
                                              is what to investigate.
"""
import numpy as np

DELTA = 146

cap = np.load("/tmp/dsp_cap.npy")
cL = cap[0].astype(np.uint32)
cR = cap[1].astype(np.uint32)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
twL = exp[0].astype(np.uint32)
twR = exp[1].astype(np.uint32)
del exp

n = min(len(cL), len(twL) - DELTA)
off = 1 << 20                      # skip the marker-step-response head
idx = np.arange(off, n, dtype=np.int64)

# compare on the 24-bit field both sides use
aL = cL[idx] & np.uint32(0xFFFFFF)
bL = (twL[idx + DELTA] >> np.uint32(8)) & np.uint32(0xFFFFFF)
aR = cR[idx] & np.uint32(0xFFFFFF)
bR = (twR[idx + DELTA] >> np.uint32(8)) & np.uint32(0xFFFFFF)

for tag, a, b in (("L", aL, bL), ("R", aR, bR)):
    d = a.astype(np.int64) - b.astype(np.int64)
    exact = int((d == 0).sum())
    tot = len(d)
    print(f"\n{tag}: {tot:,} frames compared at delta {DELTA}")
    print(f"  exactly equal : {exact:,} ({100.0*exact/tot:.3f}%)")
    ad = np.abs(d)
    for k in (1, 2, 3, 5, 10, 50, 100, 1000):
        print(f"  |diff| <= {k:>5}: {100.0*(ad<=k).mean():7.3f}%")
    print(f"  median |diff| {int(np.median(ad)):,}   mean {ad.mean():,.1f}   "
          f"max {int(ad.max()):,}")
    v, c = np.unique(d[:2_000_000], return_counts=True)
    o = np.argsort(-c)[:8]
    print("  modal diffs:", ", ".join(f"{int(v[t]):+d}x{int(c[t])}" for t in o))
    # is the difference a constant offset, or a scale?
    m = np.abs(d) < 10000
    if m.sum() > 1000:
        # a constant additive offset shows as one modal diff; a scale shows as
        # diff proportional to |value|
        vv = b[m].astype(np.float64)
        dd = d[m].astype(np.float64)
        r = np.corrcoef(vv, dd)[0, 1] if vv.std() > 0 else 0.0
        print(f"  corr(value, diff) = {r:+.3f}   "
              + ("-> SCALE-like (missing multiply)" if abs(r) > 0.3
                 else "-> ADDITIVE-like (missing add/round stage)"))

print("\nREAD: |diff|<=2 above ~90% means the twin is missing a late stage and")
print("     the comparison is otherwise correct. A flat |diff| means the capture")
print("     is not this stream and the 1.18% spike was an artifact.")
