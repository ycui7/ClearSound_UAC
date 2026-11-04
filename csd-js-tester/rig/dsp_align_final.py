#!/usr/bin/env python3
"""The alignment gate, done right. Offline, on the saved capture.

WHY THIS FILE EXISTS. Every earlier probe in this session reported "nothing
matched" when it had actually tried NOTHING: they built candidate offsets from
one or two anchor values searched in a PREFIX of the schedule, found nothing
there, and fell through to print a -1 score as if it were a measurement. -1
means "no candidate was tried". It is not a result, and presenting it as one
cost hardware time twice.

The locator here builds a real index ONCE:
    sorted values + argsort  ->  every schedule position holding a given value
Then candidate offsets come from the first NANCHOR kept frames (each of whose
values has a real position list), and each candidate is scored over a long
window. A probe that reports a score always shows how many candidates it tried.
"""
import numpy as np
import sys

WIN = 16384
NANCHOR = 60
MAXCAND = 400

cap = np.load("/tmp/dsp_cap.npy")
cL, cR = cap[0].astype(np.uint32), cap[1].astype(np.uint32)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
fL = (exp[0].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
fR = (exp[1].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
del exp

zero = (cL == 0) & (cR == 0)
print(f"capture {len(cL):,} frames, zero frames {100.0*zero.mean():.3f}%")
real = ~zero
kL, kR = cL[real], cR[real]
print(f"kept {len(kL):,}")


def build_index(sch, tag):
    srt = np.sort(sch)
    order = np.argsort(sch, kind="stable")

    def positions(v):
        lo = np.searchsorted(srt, v, side="left")
        hi = np.searchsorted(srt, v, side="right")
        return order[lo:hi]
    print(f"  indexed {tag}")
    return positions


def gate(capL, capR, posL, posR, tag):
    """Best (matches, delta) over candidates from the first NANCHOR frames."""
    n = min(WIN, len(capL))
    segL, segR = capL[:n], capR[:n]
    cands = []
    for a in range(NANCHOR):
        p = posL(int(segL[a]))
        if len(p) == 0 or len(p) > 500:      # >500 = too common to anchor
            continue
        cands.extend(int(x) - a for x in p[:20])
    tried = 0
    best = (0, 0, 0)
    seen = set()
    for d in cands:
        if d in seen or d < 0 or d + n > len(fL):
            continue
        seen.add(d)
        tried += 1
        if tried > MAXCAND:
            break
        m = int(np.count_nonzero((segL == fL[d:d + n]) &
                                 (segR == fR[d:d + n])))
        if m > best[0]:
            best = (m, d, tried)
    m, d, tried = best
    print(f"  {tag}: {m:,}/{n:,} = {100.0*m/n:.3f}% at delta={d:,} "
          f"({tried} candidates tried)")
    if tried == 0:
        print("    NO CANDIDATES -- this is a PROBE failure, not a result")
    return m, d, n


posL = build_index(fL, "filtered L")
posR = build_index(fR, "filtered R")

print("\nalignment from the head of the kept capture:")
mL, dL, n = gate(kL, kR, posL, posR, "cap(L,R) vs filtered (L,R)")
if mL < 0.99 * n:
    # second attempt: anchor mid-stream, in case the head is prefill residue
    for off in (len(kL) // 4, len(kL) // 2):
        segL, segR = kL[off:off + n], kR[off:off + n]
        if len(segL) < n:
            continue
        print(f"\nalignment from kept {off:,}:")
        mL2, dL2, _ = gate(segL, segR, posL, posR, "cap(L,R) vs filtered (L,R)")
        if mL2 >= 0.99 * n:
            mL, dL = mL2, dL2
            break

if mL >= 0.99 * n:
    print(f"\nALIGNED at delta={dL:,}. Verifying the whole kept stream...")
    i = np.arange(len(kL), dtype=np.int64)
    j = i + dL
    ok = j < len(fL)
    j = j[ok]
    bad = np.flatnonzero((kL[ok] != fL[j]) | (kR[ok] != fR[j]))
    print(f"compared {len(j):,} frames: {len(bad):,} defective "
          f"({100.0*len(bad)/max(1,len(j)):.6f}%)")
    if len(bad) == 0:
        print("\nVERDICT: DSP PATH BIT-PERFECT on every frame delivered.")
    else:
        k = int(bad[0])
        print(f"first bad at kept {k:,} (schedule {int(j[k]):,}): "
              f"got 0x{int(kL[ok][k]):06X}/0x{int(kR[ok][k]):06X} "
              f"want 0x{int(fL[j[k]]):06X}/0x{int(fR[j[k]]):06X}")
        st = np.diff(bad)
        v, c = np.unique(st, return_counts=True)
        o = np.argsort(-c)[:5]
        print("gaps between mismatches:", ", ".join(f"{int(v[t])}x{int(c[t])}" for t in o))
        ph = j[bad] % 672
        h = np.bincount(ph, minlength=672)
        t = np.argsort(-h)[:5]
        print("mismatch phase mod 672:", ", ".join(f"{int(x)}x{int(h[x])}" for x in t))
    sys.exit(0 if len(bad) == 0 else 1)

print("\nNot aligned even with a real index. Next question is which word the")
print("tap hands over (pre-scale, post-scale, or port-format), not whether the")
print("filter is right.")
