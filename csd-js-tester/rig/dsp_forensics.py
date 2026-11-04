#!/usr/bin/env python3
"""Offline forensics on a VOID DSP run: no device, no re-run.

Answers three questions the hardware run raised, from the saved capture:
  1. Where does the capture stop being real (sticky tail), i.e. when did the
     device stop producing? A hard cost overrun fails at frame 1; a late
     failure is a different bug with a different owner.
  2. Does a WIDE offset search match the filtered expectation at all? The
     anchor probed 24 sampled positions; if it missed, the question is whether
     no offset works or whether the probe was unlucky.
  3. Do L and R agree on the same schedule index? Each channel matching the
     filtered schedule somewhere, but no single index matching both, says the
     pairing/state is wrong rather than the arithmetic.
"""
import numpy as np
import sys
sys.path.insert(0, ".")

cap = np.load("/tmp/dsp_cap.npy")
cL, cR = cap[0], cap[1]
print(f"capture {len(cL):,} frames")

# ---- 1. sticky tail / zero head -------------------------------------------
def run_of(v, arr, from_end=False):
    n = len(arr)
    if from_end:
        i = n - 1
        while i > 0 and arr[i - 1] == arr[i]:
            i -= 1
        return n - i, int(arr[i])
    i = 0
    while i < n - 1 and arr[i + 1] == arr[i]:
        i += 1
    return i + 1, int(arr[0])

zh, zv = run_of(None, cL)
tl, tv = run_of(None, cL, from_end=True)
print(f"head: first {zh:,} frames all 0x{zv:06X}")
print(f"tail: last {tl:,} frames all 0x{tv:06X}")
# where real data lives
nz = np.flatnonzero(cL[:-1] != cL[1:])
last_change = int(nz[-1]) if len(nz) else -1
print(f"last change at capture {last_change:,} -> device produced ~{last_change:,} "
      f"frames then froze ({100.0*last_change/len(cL):.1f}% of the capture)")

# ---- 2/3. wide offset search, and per-channel vs paired --------------------
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
eL = (exp[0].astype(np.uint32) >> np.uint32(8)).astype(np.int32)
eR = (exp[1].astype(np.uint32) >> np.uint32(8)).astype(np.int32)
print(f"expectation {len(eL):,} frames")

WIN = 2048
for probe in (last_change // 2, last_change - 5 * WIN, last_change // 4):
    if probe < WIN:
        continue
    segL, segR = cL[probe:probe + WIN], cR[probe:probe + WIN]
    # paired: single schedule index must match both channels
    hitL = np.flatnonzero(eL == int(cL[probe]))
    hitR = np.flatnonzero(eR == int(cR[probe]))
    print(f"\nprobe @capture {probe:,}: L occurs at "
          f"{[int(x) for x in hitL[:5]]} ({len(hitL)} places), "
          f"R at {[int(x) for x in hitR[:5]]} ({len(hitR)} places)")
    cand = set(hitL.tolist())
    paired = [int(k) for k in hitL if 0 <= k < len(eR) and int(eR[k]) == int(cR[probe])]
    print(f"  indices where BOTH match at frame {probe}: {paired[:5]}")
    best = (-1, None)
    for k in list(hitL)[:20]:
        k = int(k)
        if k + WIN > len(eL):
            continue
        m = int(np.count_nonzero((segL == eL[k:k + WIN]) & (segR == eR[k:k + WIN])))
        if m > best[0]:
            best = (m, k - probe)
    print(f"  best paired run from those candidates: {best[0]}/{WIN} at delta={best[1]}")
    # channel-independence test: how good is each channel alone?
    bl = max((int(np.count_nonzero(segL == eL[k:k + WIN])) for k in list(hitL)[:20]),
             default=-1)
    br = max((int(np.count_nonzero(segR == eR[k:k + WIN])) for k in list(hitR)[:20]),
             default=-1)
    print(f"  best L-only run {bl}/{WIN}, best R-only run {br}/{WIN}")
    if bl > 0.9 * WIN and best[0] < 0.5 * WIN:
        print("  <== L alone tracks the filtered stream exactly while the PAIR "
              "does not: the two channels are filtered with different state "
              "(or L/R are swapped/independent), not an arithmetic error")
    if bl < 0.5 * WIN and br < 0.5 * WIN:
        print("  <== neither channel tracks the filtered expectation: this is "
              "the twin (or the applied preset), not the pairing")
