#!/usr/bin/env python3
"""Do L and R sit at different, drifting schedule offsets?

Real measurements on the saved capture (no device):
  * 14.35% of frames are zero in BOTH channels, runs of exactly 96, spacing
    672 -> a periodic dropout, separate defect;
  * per channel, 93-94% of capture values are members of the corresponding
    filtered schedule, against a 34.3% chance floor for that set's coverage
    -> most kept frames really are values our filtered stream contains;
  * ZERO of 100,000 (L,R) pairs equal any schedule pair, and locating kept
    frames by schedule-unique values gives no modal delta (p50 ~10.5M)
    -> the kept subsequence is not contiguous, and L/R are not taken from the
    same schedule frame.

Set membership without positional alignment means the stream is re-timed and
the two channels are re-timed DIFFERENTLY. The async USB input path in XUA
adapts the ADC stream to the USB clock; if that adaptation (sample
repeat/drop, or an independent per-channel pointer) runs per channel, then
    capL[i] = filteredL[i + dL(i)],  capR[i] = filteredR[i + dR(i)]
with dL and dR drifting apart -- exactly the observed signature.

Test: locate kept frames whose value is UNIQUE in the schedule (unambiguous
position) separately per channel, then plot dL(i) and dR(i) along the capture.
  flat and equal          -> my alignment probes were simply wrong
  flat but different      -> fixed channel skew (word-select / one-slot tap)
  drifting apart          -> per-channel re-timing on the ADC->USB path
  no located frames       -> values are not really ours (set test was chance)

Also classifies each kept frame as filtered-pair / unfiltered-pair / mixed /
neither, vectorised, to see whether the FIR was bypassed for part of the run
(the DoP latch and the marker blocks are both capable of that).
"""
import numpy as np
import sys

sys.path.insert(0, ".")
N_LOC = 400_000          # kept frames to locate per channel
NBIN = 12

cap = np.load("/tmp/dsp_cap.npy")
cL, cR = cap[0].astype(np.uint32), cap[1].astype(np.uint32)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
fL = (exp[0].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
fR = (exp[1].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
del exp

import one_shot_block as O
n_frames = len(fL)
rL, rR, _ = O.build_block(n_frames)
rL = (rL.astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
rR = (rR.astype(np.uint32) >> np.uint32(8)).astype(np.uint32)

real = ~((cL == 0) & (cR == 0))
idx = np.flatnonzero(real)
kL, kR = cL[real], cR[real]
del cL, cR
print(f"kept {len(kL):,} frames")


def unique_index(sch, tag):
    u, first, cnt = np.unique(sch, return_index=True, return_counts=True)
    ok = cnt == 1
    u, p = u[ok], first[ok]
    o = np.argsort(u)
    u, p = u[o], p[o]
    print(f"  {tag}: {len(u):,} schedule values are unique "
          f"({100.0*len(u)/len(sch):.1f}% of frames)")
    return u, p


def locate(capv, u, p, n):
    seg = capv[:n]
    loc = np.searchsorted(u, seg)
    loc = np.clip(loc, 0, max(0, len(u) - 1))
    hit = (u[loc] == seg) & (len(u) > 0)
    return np.flatnonzero(hit), p[loc[hit]]


uL, pL = unique_index(fL, "filtered L")
uR, pR = unique_index(fR, "filtered R")
iL, posL = locate(kL, uL, pL, N_LOC)
iR, posR = locate(kR, uR, pR, N_LOC)
print(f"located: L {len(iL):,}/{N_LOC:,} ({100*len(iL)/N_LOC:.1f}%), "
      f"R {len(iR):,}/{N_LOC:,} ({100*len(iR)/N_LOC:.1f}%)")

if len(iL) < 500 or len(iR) < 500:
    sys.exit("too few located frames to characterise the offsets")

dL = posL - iL
dR = posR - iR


def summ(tag, d):
    print(f"  {tag}: p5 {np.percentile(d,5):,.0f}  p50 {np.percentile(d,50):,.0f}  "
          f"p95 {np.percentile(d,95):,.0f}  spread {np.percentile(d,95)-np.percentile(d,5):,.0f}")


print("\noffset along the capture (schedule position - kept index):")
summ("dL", dL)
summ("dR", dR)

print(f"\nbinned (first {N_LOC:,} kept frames, {NBIN} bins):")
print("   bin   kept-range      dL median      dR median     dR-dL median")
for b in range(NBIN):
    lo, hi = b * N_LOC // NBIN, (b + 1) * N_LOC // NBIN
    mL = (iL >= lo) & (iL < hi)
    mR = (iR >= lo) & (iR < hi)
    if mL.sum() < 20 or mR.sum() < 20:
        continue
    medL = np.median(dL[mL])
    medR = np.median(dR[mR])
    # per-frame skew: interpolate R's offset onto L's indices
    skew = np.median(np.interp(iL[mL], iR, dR) - dL[mL])
    print(f"   {b:3d}  {lo:>8,}-{hi:<8,} {medL:>13,.0f} {medR:>14,.0f} "
          f"{skew:>14,.0f}")

# step structure: adjacent located frames' schedule steps, per channel
def steps(ii, pp, tag):
    c = np.flatnonzero(np.diff(ii) == 1)
    s = np.diff(pp[c])
    v, cc = np.unique(s, return_counts=True)
    o = np.argsort(-cc)[:5]
    print(f"  {tag}: steps {', '.join(f'{int(v[t]):+d}x{int(cc[t])}' for t in o)}")


print("\nstep between adjacent kept frames (1 = contiguous):")
steps(iL, posL, "L")
steps(iR, posR, "R")

# ---- class of each kept frame: filtered pair, unfiltered pair, mixed -------
print("\nframe class (does the pair exist in filtered / unfiltered schedule?):")
pair_filt = np.zeros(len(kL), dtype=bool)
pair_unf = np.zeros(len(kL), dtype=bool)
CH = 2_000_000
for s in range(0, min(len(kL), 4_000_000), CH):
    e = min(s + CH, len(kL))
    aL, aR = kL[s:e], kR[s:e]
    inL_f = np.isin(aL, np.unique(fL))
    inR_f = np.isin(aR, np.unique(fR))
    inL_u = np.isin(aL, np.unique(rL))
    inR_u = np.isin(aR, np.unique(rR))
    pair_filt[s:e] = inL_f & inR_f
    pair_unf[s:e] = inL_u & inR_u & ~pair_filt[s:e]
n = min(len(kL), 4_000_000)
print(f"  filtered-pair membership : {100.0*pair_filt[:n].mean():6.2f}%")
print(f"  unfiltered-pair only     : {100.0*pair_unf[:n].mean():6.2f}%")
print(f"  neither                  : {100.0*(~(pair_filt|pair_unf))[:n].mean():6.2f}%")
ph = idx[:n] % 672
for nm, mask in (("filtered", pair_filt[:n]), ("unfilt-only", pair_unf[:n]),
                 ("neither", ~(pair_filt | pair_unf)[:n])):
    if mask.sum() < 100:
        continue
    h = np.bincount(ph[mask], minlength=672)
    t = np.argsort(-h)[:3]
    conc = 100.0 * h[t].sum() / mask.sum()
    print(f"  {nm:12s} phase concentration in 3 of 672 buckets: {conc:5.1f}% "
          f"(uniform would be {300/672:.1f}%)")
