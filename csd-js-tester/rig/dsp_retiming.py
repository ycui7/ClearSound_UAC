#!/usr/bin/env python3
"""Is the ADC leg re-timing the stream? (samples held / dropped)

Established offline:
  * capture values are members of the FILTERED schedule at 94% vs a 34%
    chance floor, and the kept-frame spectrum carries the -6.8 dB shelf, so
    the DSP ran and the ADC really is hearing the DAC's data;
  * yet no constant offset aligns consecutive frames (best run 1/2048).

That pair of facts means the kept subsequence is not contiguous in the
schedule: the ADC path repeats or skips samples, so schedule_index -
capture_index is not constant. Measure the step distribution directly.

Method: take schedule values that occur EXACTLY ONCE (so a capture value
locates its schedule position unambiguously), walk the kept capture, and
histogram the jump between successive located positions.
  step 1 everywhere -> contiguous: my earlier alignment code was wrong
  step 0/2 mix      -> held/dropped samples: the ADC leg re-times
  no steps at all   -> values are not located: they were never our stream
"""
import numpy as np

cap = np.load("/tmp/dsp_cap.npy")
cL, cR = cap[0].astype(np.uint32), cap[1].astype(np.uint32)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
uL = (exp[0].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
uR = (exp[1].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
del exp

# pair-key the schedule so L and R must agree (kills 24-bit collisions)
key = uL.astype(np.uint64) << np.uint32(24) | uR.astype(np.uint64)
del uL, uR
print(f"schedule {len(key):,} frames, {len(np.unique(key)):,} distinct keys")

NKEEP = 200_000
real = ~((cL == 0) & (cR == 0))
idx = np.flatnonzero(real)[:NKEEP]
ckey = (cL[idx].astype(np.uint64) << np.uint32(24) | cR[idx].astype(np.uint64))
del cL, cR

uk, first, cnt = np.unique(key, return_index=True, return_counts=True)
uniq_ok = cnt == 1                       # only unambiguous positions
uk_un, pos_un = uk[uniq_ok], first[uniq_ok]
print(f"unambiguous schedule keys: {len(uk_un):,}")

loc = np.searchsorted(uk_un, ckey)
valid = (loc < len(uk_un))
valid[valid] &= (uk_un[loc[valid]] == ckey[valid])
found = np.flatnonzero(valid)
sched = pos_un[loc[found]]
print(f"located {len(found):,}/{len(ckey):,} kept capture frames "
      f"({100*len(found)/len(ckey):.1f}%) by unique (L,R) key")
if len(found) < 1000:
    raise SystemExit("cannot locate the capture in the schedule at all")

consec = np.flatnonzero(np.diff(found) == 1)
steps = np.diff(sched[consec])
vals, cnts = np.unique(steps, return_counts=True)
order = np.argsort(-cnts)[:10]
print("step between located schedule positions of adjacent kept frames:")
print("  " + ", ".join(f"step {vals[t]:+d}: {cnts[t]:,}" for t in order))
tot = steps.size
one = int(cnts[vals == 1][0]) if (vals == 1).any() else 0
print(f"  contiguous (step==1) fraction: {100.0*one/max(1,tot):.2f}%")
for p, lbl in ((0, "held (sample repeated)"), (2, "one dropped")):
    c = int(cnts[vals == p][0]) if (vals == p).any() else 0
    print(f"  step {p} ({lbl}): {c:,} = {100.0*c/max(1,tot):.2f}%")

d = sched - np.arange(len(sched))
print(f"schedule-minus-index: median {np.median(d):,.0f}, "
      f"min {d.min():,}, max {d.max():,}, spread {np.percentile(d,99)-np.percentile(d,1):,.0f}")
dv, dc = np.unique(d, return_counts=True)
o = np.argsort(-dc)[:5]
print("  most common deltas:", ", ".join(f"{dv[t]:,}x{dc[t]}" for t in o))
