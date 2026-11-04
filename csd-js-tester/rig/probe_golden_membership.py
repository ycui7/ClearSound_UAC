#!/usr/bin/env python3
"""Is the captured word from the schedule at all, and is L/R pairing intact?

Why this exists: the gate reported bit-exact 0/0 -- no captured pair was found
anywhere in the golden array. Two very different causes produce that, and they
need different fixes:

  (a) CHANNEL DE-PAIRING. The tap holds one word per channel independently, so
      a capture frame can carry L from schedule frame i and R from frame j. No
      pair search finds that, even though every word is genuine. Test: is each
      channel's word present in ITS OWN golden channel?
  (b) NOT OUR DATA. The words are not in the schedule at all -- stale words,
      ADC data, or a scaling/volume stage applied to them. Test: same membership
      check fails, and a scale sweep looks for L*k matching the schedule.

Also checks channel swap (L present in the R schedule).

Run against a live RAM-loaded board.
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


def in_golden(cand_words, gold_words):
    """Fraction of cand_words that appear anywhere in gold_words."""
    c = cand_words[cand_words != 0]
    if len(c) == 0:
        return 0.0, 0
    s = np.sort(np.unique(gold_words))
    idx = np.clip(np.searchsorted(s, c), 0, len(s) - 1)
    hit = s[idx] == c
    return float(hit.mean()), int(hit.sum())


def main():
    g = load_gate()
    g.pin_path()
    print("\n=== 2 s of noise: compare capture with the golden schedule ===",
          flush=True)
    r, (gL, gR) = g.stream("noise", 2.0)
    L, R = g.words(r["chunks"])
    print(f"  captured {len(L):,} frames, tap fill {float((L != 0).mean()):.4f}")
    print(f"  golden   {len(gL):,} frames")

    fL, nL = in_golden(L, gL)
    fR, nR = in_golden(R, gR)
    print(f"  L in golden L : {fL:.4f}  ({nL:,} words)")
    print(f"  R in golden R : {fR:.4f}  ({nR:,} words)")
    fLR, _ = in_golden(L, gR)
    fRL, _ = in_golden(R, gL)
    print(f"  L in golden R : {fLR:.4f}   (high => channels swapped)")
    print(f"  R in golden L : {fRL:.4f}")

    print("\n  scale sweep -- does (L*k) & 0xFFFFFF land in golden L?")
    for k in (1, 2, 4, 8, 16, 128, 256, 512, 1024, 32768):
        cand = L[L != 0]
        if len(cand) == 0:
            break
        c2 = ((cand.astype(np.int64) * k) & 0xFFFFFF).astype(np.int64)
        f, _ = in_golden(c2, gL)
        flag = "  <== MATCH" if f > 0.9 else ""
        print(f"    k={k:>5}: {f:.4f}{flag}")

    print("\n  captured L[:10]:", [hex(int(v)) for v in L[:10]])
    print("  captured R[:10]:", [hex(int(v)) for v in R[:10]])
    print("  golden   L[:10]:", [hex(int(v)) for v in gL[:10]])

    nz = int((L != 0).sum())
    if nz == 0:
        print("\n  VERDICT: capture is all zeros -- the tap never substituted.")
    elif fL > 0.9 and fR > 0.9:
        print("\n  VERDICT (a): every word IS genuine schedule data; the pair "
              "search fails because L and R are held independently, so a "
              "capture frame can mix two schedule frames. Check per channel, "
              "not per pair.")
    elif max(fL, fR, fLR, fRL) < 0.05:
        print("\n  VERDICT (b): the captured words are NOT in the schedule at "
              "all. Not our data -- stale hold, ADC, or a gain stage. Do not "
              "call this bit-exact or non-bit-exact until identified.")
    else:
        print("\n  VERDICT: partial membership -- look at the scale sweep and "
              "the swap numbers before concluding.")


if __name__ == "__main__":
    main()
