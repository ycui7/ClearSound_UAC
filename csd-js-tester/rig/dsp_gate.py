#!/usr/bin/env python3
"""The alignment gate, done right. Offline, on the saved capture.

THREE BUGS THIS FILE FIXES, each of which made a probe report a result it
never measured:

 1. NO-CANDIDATE PROBES. Earlier probes derived candidate offsets from one or
    two anchor values searched in a PREFIX of the schedule, found nothing, and
    printed the initial score -1 as if it were a measurement. -1 means "no
    candidate was tried". Such a run now says so and exits nonzero as a PROBE
    failure, so it can never be read as a device verdict.

 2. THE HEAD IS NOT AUDIO. Measured on the 800 MHz capture: the first kept
    frames are a smooth ramp (0x77757, 0x77898, 0x779D9, +321/frame) -- start-up
    residue, not the noise stimulus. Anchoring on frame 0 anchors on data that
    is not in the schedule at all. Anchors are taken from mid-stream windows.

 3. NEGATIVE DELTAS ARE REAL. The capture (23.20M frames) is LONGER than the
    block (23.07M), so schedule_pos - kept_index can be negative. An earlier
    `if d < 0: continue` threw those away and then reported "nothing matched".
    Both bounds are computed explicitly here -- numpy also wraps negative
    indices, which would otherwise compare the tail of the schedule as if valid.

Index once, score many: sorted values + argsort give every schedule position
holding a value, so candidate offsets are real positions, not guesses.
"""
import numpy as np
import sys

WIN = 16384
NANCHOR = 400
MAXCAND = 600

cap = np.load("/tmp/dsp_cap.npy")
cL, cR = cap[0].astype(np.uint32), cap[1].astype(np.uint32)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
fL = (exp[0].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
fR = (exp[1].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
del exp

zero = (cL == 0) & (cR == 0)
real = ~zero
kL, kR = cL[real], cR[real]
print(f"capture {len(cL):,} frames; zeros {100.0*zero.mean():.3f}%; "
      f"kept {len(kL):,}; schedule {len(fL):,}")


class Idx:
    """value -> every schedule position holding it."""
    def __init__(self, sch):
        self.srt = np.sort(sch)
        self.order = np.argsort(sch, kind="stable")

    def positions(self, v):
        lo = np.searchsorted(self.srt, v, side="left")
        hi = np.searchsorted(self.srt, v, side="right")
        return self.order[lo:hi]


ixL = Idx(fL)
print("indexed")


def gate(off):
    """Score candidate deltas for the kept window starting at `off`."""
    n = min(WIN, len(kL) - off)
    if n < 4096:
        return 0, 0, 0, 0
    segL, segR = kL[off:off + n], kR[off:off + n]
    seen, tried = set(), 0
    for a in range(NANCHOR):
        p = ixL.positions(int(segL[a]))
        if len(p) == 0 or len(p) > 200:
            continue                          # absent, or too common to anchor
        for x in p[:20]:
            seen.add(int(x) - (off + a))      # schedule_pos = kept_index + d
        if len(seen) > MAXCAND:
            break
    tried = len(seen)
    best = (0, 0)
    for d in seen:
        lo_i, hi_i = max(0, -d), min(n, len(fL) - d)
        if hi_i <= lo_i:
            continue
        idx = np.arange(lo_i, hi_i, dtype=np.int64)
        m = int(np.count_nonzero((segL[idx] == fL[idx + d]) &
                                 (segR[idx] == fR[idx + d])))
        if m > best[0]:
            best = (m, d)
    m, d = best
    print(f"  kept@{off:,}: {m:,}/{n:,} = {100.0*m/n:.3f}% at delta={d:,} "
          f"({tried} candidates)")
    if tried == 0:
        print("    *** NO CANDIDATES -- probe failure, NOT a device result ***")
    return m, d, n, tried


print("\nalignment attempts:")
res = [gate(o) for o in (1 << 20, 4 << 20, 8 << 20, 12 << 20, 16 << 20, 20 << 20)
       if o < len(kL) - 4096]

if all(r[3] == 0 for r in res):
    sys.exit("\nEvery attempt tried zero candidates: the probe is broken and "
             "the device is unmeasured. Fix the probe before hardware time.")

bi = max(range(len(res)), key=lambda t: res[t][0] / max(1, res[t][2]))
m, d, n, tried = res[bi]
if m < 0.99 * n:
    print(f"\nNo window aligns (best {100.0*m/n:.3f}% over {tried} real "
          "candidates). Values are members of the filtered set (~94%) but no "
          "constant offset makes a run: the stream is re-timed, not delayed.")
    sys.exit(1)

print(f"\nALIGNED at delta={d:,}. Verifying the whole kept stream...")
i = np.arange(len(kL), dtype=np.int64)
j = i + d
ok = (j >= 0) & (j < len(fL))
j = j[ok]
aL, aR = kL[ok], kR[ok]
bad = np.flatnonzero((aL != fL[j]) | (aR != fR[j]))
print(f"compared {len(j):,} frames: {len(bad):,} defective "
      f"({100.0*len(bad)/max(1,len(j)):.6f}%)")
if len(bad) == 0:
    print("\nVERDICT: DSP PATH BIT-PERFECT on every frame delivered "
          "(800 MHz, preset 2, mixer on).")
    sys.exit(0)
k = int(bad[0])
print(f"first bad at kept {k:,} (schedule {int(j[k]):,}): "
      f"got 0x{int(aL[k]):06X}/0x{int(aR[k]):06X}  "
      f"want 0x{int(fL[j[k]]):06X}/0x{int(fR[j[k]]):06X}")
v, c = np.unique(np.diff(bad), return_counts=True)
o = np.argsort(-c)[:5]
print("gaps between mismatches:", ", ".join(f"{int(v[t])}x{int(c[t])}" for t in o))
h = np.bincount(np.mod(j[bad], 672), minlength=672)
print("mismatch phase mod 672:",
      ", ".join(f"{int(x)}x{int(h[x])}" for x in np.argsort(-h)[:5]))
sys.exit(1)
