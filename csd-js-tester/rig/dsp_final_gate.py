#!/usr/bin/env python3
"""Final gate: per-channel delta histogram, then defects at the modal delta.

The impulse fit settled what the capture is: fitted h is a delta (center tap
1.000, others <=0.002) and 92.6% of frames match the FILTERED twin exactly, so
the loopback is a digital identity copy of our own filtered stream and the DSP
arithmetic is right. My earlier "no alignment" results were a probe bug -- the
anchor value never occurred in the searched prefix, so zero candidates were
ever scored and the loop printed "nothing matched".

Now measure the index map instead of guessing it: for capture values that are
UNIQUE in the schedule (unambiguous position), histogram (schedule_pos - i).
The modal delta is the true one; everything off-modal is the ~7% minority and
gets characterized by phase, not assumed away.
"""
import numpy as np

cap = np.load("/tmp/dsp_cap.npy")
cL, cR = cap[0].astype(np.uint32), cap[1].astype(np.uint32)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
fL = (exp[0].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
fR = (exp[1].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
del exp

real = ~((cL == 0) & (cR == 0))
idx = np.flatnonzero(real)
kL, kR = cL[real], cR[real]
del cL, cR
print(f"kept {len(kL):,} frames")

N = 2_000_000                      # sample size for the delta census
sl = np.argsort(fL)
srtL = fL[sl]
# unique positions: count occurrences via sorted-diff trick
uniL, firstL, cntL = np.unique(fL, return_index=True, return_counts=True)
uL_ok = uniL[cntL == 1]
pL_ok = firstL[cntL == 1]
o = np.argsort(uL_ok)
uL_ok, pL_ok = uL_ok[o], pL_ok[o]
print(f"unambiguous schedule L values: {len(uL_ok):,} "
      f"({100.0*len(uL_ok)/len(fL):.0f}% of frames are unique)")

samp = kL[:N]
loc = np.searchsorted(uL_ok, samp)
loc = np.clip(loc, 0, len(uL_ok) - 1)
hit = (uL_ok[loc] == samp)
pos = pL_ok[loc[hit]]
i = np.flatnonzero(hit)
d = pos - i
vals, cnts = np.unique(d, return_counts=True)
o2 = np.argsort(-cnts)[:8]
print(f"located {hit.sum():,}/{N:,} ({100.0*hit.sum()/N:.1f}%) by unique L value")
print("modal deltas (schedule_pos - kept_index):")
for t in o2:
    print(f"   delta {vals[t]:>12,}: {cnts[t]:,} frames "
          f"({100.0*cnts[t]/max(1,hit.sum()):.2f}%)")
mode = int(vals[o2[0]])
inmode = np.flatnonzero(d == mode)
print(f"\nmodal delta {mode:,} covers {len(inmode):,} of {len(d):,} located "
      f"({100.0*len(inmode)/max(1,len(d)):.1f}%)")

# Are the located deltas contiguous in capture order, or do they jump?
print(f"delta spread: p1 {np.percentile(d,1):,.0f}  p50 {np.percentile(d,50):,.0f}  "
      f"p99 {np.percentile(d,99):,.0f}")

# With the modal delta fixed, exact-match rate for BOTH channels.
i_all = np.arange(len(kL), dtype=np.int64)
j = i_all + mode
ok = (j >= 0) & (j < len(fL))
j = j[ok]
aL, aR = kL[ok], kR[ok]
matchL = (aL == fL[j])
matchR = (aR == fR[j])
print(f"\nat delta {mode:,}: L exact {100.0*matchL.mean():.3f}%, "
      f"R exact {100.0*matchR.mean():.3f}%, both {100.0*(matchL&matchR).mean():.3f}%")
bad = np.flatnonzero(~(matchL & matchR))
print(f"mismatched frames: {len(bad):,}")
if len(bad):
    ph = idx[ok][bad] % 672
    h = np.bincount(ph, minlength=672)
    top = np.argsort(-h)[:6]
    print("mismatch phase in the 672-frame period:",
          ", ".join(f"{int(t)}x{int(h[t])}" for t in top))
    # step structure of the bad frames: are they isolated or in runs?
    st = np.diff(bad)
    v2, c2 = np.unique(st, return_counts=True)
    o3 = np.argsort(-c2)[:5]
    print("gaps between consecutive mismatches:",
          ", ".join(f"{int(v2[t])}x{int(c2[t])}" for t in o3))
