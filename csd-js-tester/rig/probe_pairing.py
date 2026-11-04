#!/usr/bin/env python3
"""Is the capture one schedule word duplicated to both channels, or a small
stale pool being replayed? Decide by counting and by walking, not by membership.

The membership probe said every captured word is in the golden schedule -- for
BOTH channels in BOTH schedules (1.0000 four ways), and captured L[:10] equals
captured R[:10] exactly. Three causes give that, and they are diagnosed
differently:

  1. BOTH DAC BUSSES CARRY THE SAME WORD. The mixer would be mapping host L to
     both outputs (or hostL+hostR). Then captured L == captured R == a run of
     goldenL, contiguously. Check: a contiguous walk of captured L against
     goldenL succeeds, and against goldenR it does not.
  2. A SMALL STALE POOL REPLAYED. The tap holds one word per channel and the
     audio core re-serves the same slot, so capture cycles through few distinct
     values. Check: distinct(captured L) is tiny compared with its length.
  3. REAL, PAIRED, CONTIGUOUS. captured L walks goldenL and captured R walks
     goldenR at the same offset. This is the shape a bit-perfect gate needs.

Prints the counts and the walk results; the verdict names which one it is.
"""
import importlib.util
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def load_gate():
    spec = importlib.util.spec_from_file_location(
        "g", os.path.join(HERE, "csd_bitperfect_gate.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def walk(cand, gold, k0):
    """Longest contiguous match of cand[0:] against gold[k0:]. Returns length."""
    n = min(len(cand), len(gold) - k0)
    if n <= 0:
        return 0
    eq = cand[:n] == gold[k0:k0 + n]
    nz = np.nonzero(~eq)[0]
    return int(nz[0]) if len(nz) else int(n)


def main():
    g = load_gate()
    g.pin_path()
    print("\n=== 2 s of noise, per-channel structure of the capture ===",
          flush=True)
    r, (gL, gR) = g.stream("noise", 2.0)
    L, R = g.words(r["chunks"])
    n = int(len(L))
    print(f"  captured {n:,} frames   tap fill {float((L != 0).mean()):.4f}")

    print("\n  --- how many DISTINCT words? (cause 2: a small replayed pool) ---")
    for nm, arr in (("L", L), ("R", R)):
        c = arr[arr != 0]
        u = int(len(np.unique(c))) if len(c) else 0
        frac = u / max(1, len(c))
        print(f"    {nm}: {len(c):,} nonzero, {u:,} distinct = {frac:.4f}  "
              f"[~1.0 expected for noise; <<1.0 means a replayed pool]")

    print("\n  --- is L identical to R? (cause 1: one word on both busses) ---")
    same = int((L == R).mean()) if n else 0
    print(f"    L == R for {same*100:.2f}% of frames")

    print("\n  --- contiguous walk against each schedule (which schedule, and "
          "how far) ---")
    best = {}
    for cn, ca in (("capL", L), ("capR", R)):
        for gn, ga in (("goldL", gL), ("goldR", gR)):
            c = ca[ca != 0][:4096]
            if len(c) < 64:
                best[(cn, gn)] = (0, 0)
                continue
            # candidate offsets: every position in gold matching the first word
            h = np.nonzero(ga == int(c[0]))[0][:40]
            bl, bk = 0, -1
            for k in h:
                w = walk(c, ga, int(k))
                if w > bl:
                    bl, bk = w, int(k)
            best[(cn, gn)] = (bl, bk)
            print(f"    {cn} vs {gn}: longest contiguous run {bl:,} "
                  f"(at offset {bk:,})"
                  + ("   <== tracks this schedule" if bl > 2048 else ""))

    lr = best[("capL", "goldL")]
    rr = best[("capR", "goldR")]
    lx = best[("capL", "goldR")]
    rx = best[("capR", "goldL")]
    print("\n  VERDICT")
    if same > 0.99:
        print("    Cause 1: L and R carry the SAME word on 99%+ of frames. The "
              "host stream is being collapsed to one word before the tap -- "
              "look at the mixer matrix / samplesOut, not at the tap.")
    distinctL = len(np.unique(L[L != 0])) if n else 0
    if n and distinctL / max(1, int((L != 0).sum())) < 0.5:
        print("    Cause 2: capture replays a small pool -- the tap's held word "
              "is being re-served, so the audio core is not advancing.")
    if lr[0] > 2048 and rr[0] > 2048 and not (lx[0] > 2048 or rx[0] > 2048):
        print("    Cause 3: L tracks goldenL and R tracks goldenR, each "
              "contiguously. The wire IS carrying the schedule; the pair "
              "search only failed because the two channels drift apart by a "
              "frame. Verify per channel and the gate can pass.")
    if not any((lr[0] > 2048, rr[0] > 2048, lx[0] > 2048, rx[0] > 2048)):
        print("    No contiguous track in any schedule: the words are "
              "individually genuine but not delivered in schedule order. That "
              "is an ordering/serving fault, and bit-perfect must not be "
              "claimed.")


if __name__ == "__main__":
    main()
