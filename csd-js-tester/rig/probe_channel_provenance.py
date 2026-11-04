#!/usr/bin/env python3
"""Test one hypothesis: both I2S/DAC channels carry host R, and host L is lost.

Every observation so far is consistent with it and with nothing else I have:

  - full stereo noise: captured L == captured R on 100.00 % of frames
  - 1 kHz tone on host L only, R silent  -> BOTH captured channels zero
  - 1 kHz tone on host R only, L silent  -> tone on BOTH captured channels
  - longest contiguous schedule run 48 frames, which matches the 0.9674 fill
    (a hole every ~30 frames), so ordering is not implicated

If true, host L never reaches the words the audio core hands to the I2S ports.
That is a product defect (stereo shipping as mono on one channel), not a tap
artifact: the tap reports the word about to go on the bus, so if both channels
report host R, both busses carry host R.

Method. Play stereo noise and, per captured channel, ask which schedule it
tracks -- golden L or golden R -- by best-match fraction over a long window,
tolerant of the known ~3 % holes (unserved slots read 0). A channel that tracks
golden R at ~1.00 and golden L at ~0.00 is host R. Reported per channel, so the
two channels are never judged together.

Holes are handled by requiring agreement at matched indices rather than strict
contiguity: for a candidate offset k, count frames where captured == golden[k:]
excluding captured slots that are 0 (unserved). The offset is found by matching
the first non-zero captured word.
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


def track(cand, gold, k, win=200_000):
    """Agreement fraction of cand[:win] against gold[k:], ignoring captured 0
    (unserved slot reads 0 and is not a mismatch against the wire)."""
    n = min(len(cand), win, len(gold) - k)
    if n < 1000:
        return 0.0, 0
    c = cand[:n]
    gv = gold[k:k + n]
    live = c != 0
    if live.sum() < 1000:
        return 0.0, int(live.sum())
    return float((c[live] == gv[live]).mean()), int(live.sum())


def best_offset(cand, gold, tries=60, win=200_000):
    """Candidate offsets from the first few non-zero captured words."""
    nz = np.nonzero(cand != 0)[0]
    if len(nz) == 0:
        return 0, 0.0, 0
    best = (0, 0.0, 0)
    for pos in nz[:6]:
        w = int(cand[pos])
        h = np.nonzero(gold == w)[0][:tries]
        for k0 in h:
            k = int(k0) - pos
            if k < 0:
                continue
            f, n = track(cand[k:], gold, k, win) if False else track(cand, gold, k, win)
            if f > best[1]:
                best = (k, f, n)
            if f > 0.99:
                return best
    return best


def main():
    g = load_gate()
    g.pin_path()
    print("\n=== 3 s of stereo noise: which schedule does EACH captured "
          "channel track? ===", flush=True)
    r, (gL, gR) = g.stream("noise", 3.0)
    L, R = g.words(r["chunks"])
    print(f"  captured {len(L):,} frames   fill {float((L != 0).mean()):.4f}")
    print(f"  stimulus: L and R uncorrelated by seed offset 7919 per channel")

    out = {}
    for cn, ca in (("captured L", L), ("captured R", R)):
        row = {}
        for gn, ga in (("golden L", gL), ("golden R", gR)):
            k, f, n = best_offset(ca, ga)
            row[gn] = (k, f, n)
            print(f"  {cn} vs {gn}: best agreement {f:.4f} "
                  f"over {n:,} live frames (offset {k:,})")
        out[cn] = row

    print("\n  VERDICT")
    def tracks(row, gn):
        return row[gn][1] > 0.99
    def rejects(row, gn):
        return row[gn][1] < 0.02
    lR = tracks(out["captured L"], "golden R") and rejects(out["captured L"], "golden L")
    rR = tracks(out["captured R"], "golden R") and rejects(out["captured R"], "golden L")
    lL = tracks(out["captured L"], "golden L") and rejects(out["captured L"], "golden R")
    rR2 = tracks(out["captured R"], "golden R") and rejects(out["captured R"], "golden L")
    if lR and rR:
        print("    CONFIRMED: BOTH captured channels track the host R schedule "
              "and reject host L. Host L never reaches the words handed to the "
              "I2S ports -- the DAC is outputting host R on both channels. "
              "This blocks any bit-perfect claim and is a stereo defect.")
    elif lL and rR2:
        print("    CORRECT: captured L tracks golden L, captured R tracks "
              "golden R. Channels are independent; the earlier L==R must have "
              "come from an armed generator or a mono stimulus.")
    else:
        print("    NEITHER clean shape. Neither channel tracks one schedule "
              "exclusively -- do not attribute a cause until one of the two "
              "above appears. Print the four numbers above before guessing.")


if __name__ == "__main__":
    main()
