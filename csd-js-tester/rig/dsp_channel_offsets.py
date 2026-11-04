#!/usr/bin/env python3
"""Per-channel offsets, and what the non-matching minority actually is.

Pairing test said: each channel is a member of the filtered schedule (93% vs
34% chance) but ZERO of 100,000 (L,R) pairs occur in the schedule. That is the
signature of the two channels arriving at DIFFERENT schedule offsets -- an
I2S word-select/latency skew puts L at schedule k+d1 and R at k+d2, so no
single delta can align the pair and no pair key can ever match.

Test A: for each capture channel, best (schedule, index-map, delta) match
fraction over an 8192-frame window. If L and R each reach ~100% at their own
delta, the arithmetic is bit-perfect and only the pairing is broken.

Test B: classify every kept frame by membership in {filtered} vs {unfiltered}
vs neither, and histogram that class against position inside the 672-frame
dropout period. If a class concentrates in a phase window, the mixer/DoP
bypass is phase-locked, not random.
"""
import numpy as np

WIN = 8192
PERIOD = 672

cap = np.load("/tmp/dsp_cap.npy")
cL, cR = cap[0].astype(np.uint32), cap[1].astype(np.uint32)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
fL = (exp[0].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
fR = (exp[1].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
del exp
import sys
sys.path.insert(0, ".")
import one_shot_block as O
n_frames = len(fL)
rL, rR, _ = O.build_block(n_frames)
rL = rL.astype(np.uint32) >> np.uint32(8)
rR = rR.astype(np.uint32) >> np.uint32(8)

real = ~((cL == 0) & (cR == 0))
idx = np.flatnonzero(real)
kL = cL[real]
kR = cR[real]
print(f"kept {len(kL):,} frames")


def best_align(capv, sch, holes, tag):
    """Fraction-matching best delta for one channel. holes: model the 96/672
    dropout as schedule positions consumed (True) or not (False)."""
    base = np.arange(min(WIN, len(capv)), dtype=np.int64)
    if holes:
        base = base + 96 * (base // 576)
    v = int(capv[0])
    cands = np.flatnonzero(sch[:3_000_000] == v)[:60]
    best = (-1, 0)
    for c in cands:
        d = int(c) - int(base[0])
        if d < 0 or base[-1] + d >= len(sch):
            continue
        m = int(np.count_nonzero(capv[:len(base)] == sch[base + d]))
        if m > best[0]:
            best = (m, d)
    m, d = best
    print(f"  {tag}: {m:,}/{len(base):,} = {100.0*m/len(base):.2f}% at delta={d:,}")
    return m / len(base), d


print("\nTest A -- per-channel independent alignment:")
aL_h, dL_h = best_align(kL, fL, True,  "capL vs filtered L, holes")
aL_c, dL_c = best_align(kL, fL, False, "capL vs filtered L, contig")
aR_h, dR_h = best_align(kR, fR, True,  "capR vs filtered R, holes")
aR_c, dR_c = best_align(kR, fR, False, "capR vs filtered R, contig")
xL_h, _ = best_align(kL, fR, True,  "capL vs filtered R, holes (crosstalk)")
xR_h, _ = best_align(kR, fL, True,  "capR vs filtered L, holes (crosstalk)")
best_L = max(aL_h, aL_c)
best_R = max(aR_h, aR_c)
if best_L > 0.99 and best_R > 0.99:
    print("\n  ==> BOTH channels are bit-exact at their own offset: the DSP")
    print("      arithmetic is bit-perfect; only the L/R pairing is wrong.")
elif best_L > 0.99 or best_R > 0.99:
    print("\n  ==> one channel exact, the other not -- asymmetric path defect.")
else:
    print("\n  ==> neither channel aligns as a run: values match only as a set.")

print("\nTest B -- class of each kept frame, by phase in the 672 period:")
sets = {"filt": set(np.unique(np.concatenate([fL, fR])).tolist()),
        "unfilt": set(np.unique(np.concatenate([rL, rR])).tolist())}
rng = np.random.default_rng(5)
pick = rng.choice(len(kL), size=min(60_000, len(kL)), replace=False)
cls = np.empty(len(pick), dtype="<U8")
for t, s in sets.items():
    pass
cLs, cRs = kL[pick], kR[pick]
fLs, fRs = np.fromiter((int(v) in sets["filt"] for v in cLs), bool, len(cLs))
uLs, uRs = np.fromiter((int(v) in sets["unfilt"] for v in cLs), bool, len(cLs))
cls = np.where(fLs & fRs, "both-filt",
      np.where(uLs & uRs, "both-unfilt",
      np.where(fLs | fRs, "mixed", "neither")))
for name, cnt in zip(*np.unique(cls, return_counts=True)):
    print(f"  {name:12s}: {cnt:,} ({100.0*cnt/len(cls):.1f}%)")
ph = idx[pick] % PERIOD
for name in ("both-filt", "both-unfilt", "mixed", "neither"):
    m = cls == name
    if m.sum() < 50:
        continue
    h = np.bincount(ph[m], minlength=PERIOD)
    top = np.argsort(-h)[:3]
    conc = 100.0 * h[top].sum() / max(1, m.sum())
    print(f"  {name:12s} phase: top buckets {[(int(t), int(h[t])) for t in top]}"
          f"  ({conc:.0f}% of class in 3 buckets)")
