#!/usr/bin/env python3
"""L/R slot identity test -- the cheapest discriminator left.

The tap is on the TX words (csd_gen.h:373-407: 'substitutes the words it was
about to hand to the I2S DAC port into the ADC slots'), so the analog path is
NOT in this loop and the comparison should be sample-exact. Schedule pairs are
99.9996% unique and the capture matches zero of them, so the two channels are
not read from the same frame. Two shapes explain that, and they are one
boolean apart:

  both slots carry the same channel   -> capL == capR (arrays identical)
  one slot is stale by n frames       -> capL[i] == fL[i+d], capR[i] == fR[i+d+n]

Also tests the slot-offset family directly: is capR[i] == fR[i+d+1] (reader
one slot behind), which is exactly what an off-by-one in the ADC slot index
looks like.
"""
import numpy as np

cap = np.load("/tmp/dsp_cap.npy")
cL, cR = cap[0].astype(np.uint32), cap[1].astype(np.uint32)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
fL = (exp[0].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
fR = (exp[1].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
del exp

real = ~((cL == 0) & (cR == 0))
kL, kR = cL[real], cR[real]
print(f"kept {len(kL):,}")

same = int(np.count_nonzero(kL == kR))
print(f"capL == capR in {same:,}/{len(kL):,} ({100.0*same/len(kL):.2f}%)"
      + ("   <== BOTH ADC SLOTS CARRY ONE CHANNEL" if same > 0.9 * len(kL) else ""))

# how much do the two schedule channels share as value SETS? if they overlap
# heavily, single-channel membership tests prove nothing -- and that is the
# trap I fell into when I read '93% member of L and of R' as meaningful.
uL, uR = np.unique(fL), np.unique(fR)
inter = len(np.intersect1d(uL, uR))
print(f"schedule value sets: |L|={len(uL):,} |R|={len(uR):,} overlap={inter:,} "
      f"({100.0*inter/max(1,len(uL)):.1f}% of L)  "
      + ("<== sets overlap heavily: per-channel membership is NOT evidence"
         if inter > 0.5 * len(uL) else "(sets distinct: membership IS evidence)"))

# slot-offset family: best match of capR against fR at a per-frame skew, using
# an ANCHOR that actually exists (the bug in my first probes was anchoring on a
# value that never occurred in the searched prefix, so no candidate was tried).
def best_run(capv, sch, tag, win=4096, nanchor=200):
    seg = capv[:win]
    pos_of = {}
    # index every value in a window around the expected arrival, not the head
    base_guess = len(sch) - len(capv)          # prefill-sized default
    cand = []
    for a in range(nanchor):
        v = int(seg[a])
        hit = np.flatnonzero(sch == v)[:64]
        cand.extend(int(h) - a for h in hit)
    best = (-1, 0)
    for d in cand[:1500]:
        if d < 0 or d + win > len(sch):
            continue
        m = int(np.count_nonzero(seg == sch[d:d + win]))
        if m > best[0]:
            best = (m, d)
    m, d = best
    print(f"  {tag}: {m:,}/{win:,} = {100.0*max(m,0)/win:.2f}% at delta={d:,}")
    return m, d

print("\nper-channel run alignment (anchor from the whole schedule):")
mL, dL = best_run(kL, fL, "capL vs filtered L")
mR, dR = best_run(kR, fR, "capR vs filtered R")
if mL > 0.95 * 4096 and mR > 0.95 * 4096:
    print(f"  ==> both channels sample-exact at their own delta "
          f"(L {dL:,}, R {dR:,}, skew {dR-dL:+,}). DSP arithmetic is "
          "bit-perfect; the L/R slot pairing is the only defect.")
elif mL > 0.95 * 4096 or mR > 0.95 * 4096:
    print("  ==> exactly one channel sample-exact: asymmetric slot defect.")
else:
    print("  ==> neither channel aligns as a run even with real anchors.")
