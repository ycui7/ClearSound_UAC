#!/usr/bin/env python3
"""Which pairing does the capture actually use?

Facts from the saved capture (all offline, no device):
  * each channel individually is a member of the FILTERED schedule at 94%
    against a 34% chance floor, and the kept-frame spectrum carries the
    -6.8 dB shelf -> the DSP ran and the ADC is hearing the DAC's data;
  * yet ZERO of 200,000 (L,R) pairs match any schedule pair.

Exact values, wrong pairing = the reader is not taking the two channels from
the same place. Candidate layouts, keyed on the schedule and matched against
capture pairs:

  normal   schedule frame k -> (L[k], R[k])       (the contract; already 0%)
  swap                  -> (R[k], L[k])           (ADC wired L<->R)
  planarL                -> (L[k], L[k+1])        (reader strides a PLANAR
                                                   buffer as if interleaved:
                                                   every "frame" is two
                                                   consecutive L samples)
  planarR                -> (R[k], R[k+1])        (same, on the R block)

A planar match also predicts the observed non-contiguity: consecutive capture
frames would sit 2 schedule frames apart, so step==1 could never appear.
"""
import numpy as np
import sys

NKEEP = 100_000

cap = np.load("/tmp/dsp_cap.npy")
cL, cR = cap[0].astype(np.uint32), cap[1].astype(np.uint32)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
uL = (exp[0].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
uR = (exp[1].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
del exp

real = ~((cL == 0) & (cR == 0))
idx = np.flatnonzero(real)[:NKEEP]
kL = cL[idx].astype(np.uint64)
kR = cR[idx].astype(np.uint64)
ckey = kL << np.uint64(24) | kR


def locate(name, a, b):
    """Match capture pairs against schedule pairs (a[k], b[k])."""
    if len(a) != len(b):
        return
    key = (a.astype(np.uint64) << np.uint64(24)) | b.astype(np.uint64)
    uk, first, cnt = np.unique(key, return_index=True, return_counts=True)
    ok = cnt == 1
    uk_un, pos_un = uk[ok], first[ok]
    loc = np.searchsorted(uk_un, ckey)
    valid = loc < len(uk_un)
    valid[valid] &= (uk_un[loc[valid]] == ckey[valid])
    found = np.flatnonzero(valid)
    pct = 100.0 * len(found) / max(1, len(ckey))
    print(f"  {name:9s}: located {len(found):,}/{len(ckey):,} ({pct:.1f}%)")
    if len(found) > 1000:
        consec = np.flatnonzero(np.diff(found) == 1)
        steps = np.diff(pos_un[loc[found]][consec])
        vals, cnts = np.unique(steps, return_counts=True)
        o = np.argsort(-cnts)[:4]
        print(f"             steps between adjacent kept frames: "
              + ", ".join(f"{vals[t]:+d}x{cnts[t]}" for t in o))
        d = pos_un[loc[found]] - np.arange(len(found))
        print(f"             delta median {np.median(d):,.0f} "
              f"(min {d.min():,} max {d.max():,})")
    return found


print("pairing hypothesis test (0% = refuted, ~100% = the layout in use):")
locate("normal", uL, uR)
locate("swap", uR, uL)
locate("planarL", uL[:-1], uL[1:])
locate("planarR", uR[:-1], uR[1:])

# single-channel membership, both channels, both schedules: says whether the
# capture's R is carrying L data at all.
rng = np.random.default_rng(3)
pick = rng.choice(len(idx), size=20_000, replace=False)
for cname, carr in (("capL", kL[pick]), ("capR", kR[pick])):
    for sname, sarr in (("schedL", uL), ("schedR", uR)):
        uni = np.unique(sarr)
        hit = int(np.count_nonzero(np.isin(carr, uni)))
        print(f"  {cname} in {sname}: {hit}/20000 "
              f"({100.0*hit/20000:.1f}%; chance {100.0*len(uni)/(1<<24):.1f}%)")
