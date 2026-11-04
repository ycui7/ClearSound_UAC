#!/usr/bin/env python3
"""Is the capture quantized to a coarse step? The one-line test.

Measured: at the correct delta (146) the capture and the twin agree to
|diff| <= 100 on 99.9% of frames, 100% within 1000, median 20, additive
(corr(value, diff) = 0.001), while RMS matches to 1e-5 and every spectral band
matches to 0.00 dB. A bounded ADDITIVE residual of ~tens of LSBs of a 24-bit
field is the size of half an LSB of a ~17-bit word, i.e. one stage between the
filter and the tap stores the sample more coarsely than 24 bits.

If so the capture's low bits are not free: for a step of 2^k the low k bits of
every captured value are 0. Test each k directly. Also test the neighbour:
whether the DIFFERENCE itself is a multiple of the step (which says the twin is
the fine one and the device the coarse one, and by how much).
"""
import numpy as np

cap = np.load("/tmp/dsp_cap.npy")
cL = cap[0].astype(np.uint32) & np.uint32(0xFFFFFF)
cR = cap[1].astype(np.uint32) & np.uint32(0xFFFFFF)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
twL = (exp[0].astype(np.uint32) >> np.uint32(8)) & np.uint32(0xFFFFFF)
del exp

off, n = 1 << 20, 4 << 20
a = cL[off:off + n]
b = twL[off + 146: off + 146 + n]

print("low-bit structure of the CAPTURE (a coarse store forces low bits to 0):")
for k in (1, 2, 3, 4, 5, 6, 7, 8):
    z = float(np.mean((a & np.uint32((1 << k) - 1)) == 0))
    print(f"   low {k} bits all zero: {100*z:7.3f}%   "
          f"(random data would give {100.0/(1<<k):.3f}%)")

print("\nsame on the twin (what my host model produces):")
for k in (1, 2, 3, 4, 5, 6, 7, 8):
    z = float(np.mean((b & np.uint32((1 << k) - 1)) == 0))
    print(f"   low {k} bits all zero: {100*z:7.3f}%")

d = a.astype(np.int64) - b.astype(np.int64)
small = np.abs(d) <= 1000
print(f"\nat delta 146, {small.sum():,} frames with |diff|<=1000:")
for k in (1, 2, 3, 4, 5, 6, 7):
    z = float(np.mean(np.abs(d[small]) % (1 << k) == 0))
    print(f"   |diff| multiple of 2^{k}: {100*z:7.3f}%  (chance {100.0/(1<<k):.3f}%)")

# nearest-multiple check: does capture == round(twin / step) * step for some step?
print("\nbest coarse-step explanation for the capture:")
for step in (2, 4, 8, 16, 32, 64, 128, 256):
    q = ((b.astype(np.int64) + step // 2) // step) * step
    hit = float(np.mean(a.astype(np.int64) == q))
    print(f"   step {step:>4}: capture == round(twin/step)*step on {100*hit:6.3f}% of frames")
