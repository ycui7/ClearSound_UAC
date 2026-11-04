#!/usr/bin/env python3
"""Which BIT FIELD is the comparison basis? All of them, one pass.

Why this is the question now. The corrected rare-value histogram gives a real
spike (17,342 at delta 146 against ~0.28 chance) but it carries only 1.18% of
the mass, and the aligned values arrive in short ordered runs. That is exactly
the signature of comparing the same signal at a SMALL systematic value offset:
samples that happen to round identically locate at the correct position, the
other ~99% miss exact equality and scatter flat. Spectra would still match to
0.00 dB and the distinct-value counts would still nearly agree -- both observed.

So before blaming the delivery path for scrambling, rule out the cheapest
possibility: the offline tools and the device are not comparing the same field.
The tap hands over the post-DSP Q31 word (its low byte measured random), the
reader hands over a 24-bit field, and the rig's own STRICT compare uses the full
32-bit word against the full 32-bit twin. Any offline script that mixed those
bases would produce a weak spike instead of a strong one.

Every basis below is scored on the same rare-value delta test. The basis that
is right carries ~all the mass at one delta; the wrong ones give the 1%-type
ghost seen so far.
"""
import numpy as np

cap = np.load("/tmp/dsp_cap.npy")            # (2, N) as the rig saved it
rawL = cap[0].astype(np.uint32)
rawR = cap[1].astype(np.uint32)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
twL = exp[0].astype(np.uint32)               # twin, full Q31 word
twR = exp[1].astype(np.uint32)
del exp


def modal(capv, sch, tag):
    uv, uf, cf = np.unique(sch, return_index=True, return_counts=True)
    srt = np.sort(capv)
    ordv = np.argsort(capv, kind="stable")
    cand = uv[cf <= 3]
    lo = np.searchsorted(srt, cand, "left")
    hi = np.searchsorted(srt, cand, "right")
    keep = (hi > lo) & ((hi - lo) <= 3)
    pc = ordv[lo[keep]].astype(np.int64)
    pf = uf[cf <= 3][keep].astype(np.int64)
    if len(pc) < 500:
        print(f"  {tag:34s}: {len(pc):>8,} pairs -- VOID")
        return 0.0
    d = pf - pc
    v, c = np.unique(d, return_counts=True)
    o = int(np.argmax(c))
    frac = 100.0 * c[o] / len(pc)
    print(f"  {tag:34s}: {len(pc):>8,} pairs  modal {int(v[o]):>9,} "
          f"carries {c[o]:>7,} ({frac:6.2f}%)")
    return frac


print("L channel, candidate comparison bases (capture field vs twin field):")
b = {}
b["raw32 vs twin32 (rig's strict)"] = (rawL, twL)
b["raw32 vs twin32, arith >>0"]     = (rawL, twL)
b["low24(cap) vs low24(twin)"]      = (rawL & np.uint32(0xFFFFFF),
                                       twL & np.uint32(0xFFFFFF))
b["low24(cap) vs high24(twin)"]     = (rawL & np.uint32(0xFFFFFF),
                                       (twL >> np.uint32(8)) & np.uint32(0xFFFFFF))
b["high24(cap) vs high24(twin)"]    = ((rawL >> np.uint32(8)) & np.uint32(0xFFFFFF),
                                       (twL >> np.uint32(8)) & np.uint32(0xFFFFFF))
b["raw32 vs twin<<8 (wire form)"]   = (rawL, (twL << np.uint32(8)))
b["raw32 vs twin>>8"]               = (rawL, twL >> np.uint32(8))
best = max((modal(a, t, k) if a.dtype == t.dtype else 0.0, k)
           for k, (a, t) in b.items())
print("\nR channel:")
for k, (a, t) in b.items():
    modal(a, t, k)
print(f"\nbest basis: {best[1]} at {best[0]:.2f}%")
if best[0] < 50:
    print("No basis aligns the mass: the value field is not the problem, so the")
    print("delivery path really is reordering. Next: the tap/reader ring itself.")
