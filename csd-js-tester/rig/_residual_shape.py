#!/usr/bin/env python3
"""What is the ~20 LSB residual? Its spectrum names the injection point.

Corrected reading of the evidence. At delta 146 the capture matches the twin on
99.9% of frames to |diff| <= 100 and 100% to |diff| <= 1000, with the low bits
uniformly distributed (so no coarse store) and corr(value, diff) = 0.001 (so not
a gain term). My earlier "the delivery path scrambles the order" conclusion was
an artifact of a test that demanded EXACT equality: only ~1.2% of samples land
exactly on their twin value, so only those were locatable, and their 1.2% spike
at the correct delta 146 was the only real structure in that histogram.

So the stream is delivered in order at a constant delay, and the filter output
differs from my host twin by a bounded ~20 LSB (median) error. The biquad has a
pole at |z| ~ 0.9987, so a ONE-LSB difference in the state arithmetic is
amplified by 1/(1-|p|) ~ 780x -- which is precisely this magnitude. That makes
1-ULP state rounding the leading hypothesis, and it also means the vendor's
Python model cannot arbitrate it: my port was validated against
`audio_dsp.process_xcore`, so if that model differs from the actual `biquad.S`
in rounding order, both are wrong together and the device diverges exactly like
this. The arbiter has to be the shape of the error.

If the error enters the RECURSION (state rounding), it is filtered by 1/(1-z^-1)
shaped by the poles: strongly LOW-PASS -- low frequency gets amplified most.
If the error is added at the OUTPUT (dither, an extra rounding stage), it is
WHITE. If it enters before the filter, it has the SHELF shape itself.
"""
import numpy as np

DELTA = 146
N = 1 << 15

cap = np.load("/tmp/dsp_cap.npy")
cL = cap[0].astype(np.uint32) & np.uint32(0xFFFFFF)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
twL = (exp[0].astype(np.uint32) >> np.uint32(8)) & np.uint32(0xFFFFFF)
del exp


def s24(u):
    u = u & np.uint32(0xFFFFFF)
    return np.where(u >= (1 << 23), u.astype(np.float64) - (1 << 24),
                    u.astype(np.float64))


off = 1 << 20
a = s24(cL[off:off + (N << 6)])
b = s24(twL[off + DELTA: off + DELTA + (N << 6)])
m = min(len(a), len(b)) // N * N
a, b = a[:m], b[:m]
d = a - b

print(f"residual over {m:,} frames: median |d| {np.median(np.abs(d)):.0f}, "
      f"RMS {np.sqrt((d**2).mean()):.1f}, "
      f"signal RMS {np.sqrt((b**2).mean()):,.0f}")

w = np.hanning(N).astype(np.float64)
def band(x, edges):
    acc = np.zeros(N // 2 + 1)
    for s in range(len(x) // N):
        acc += np.abs(np.fft.rfft(x[s * N:(s + 1) * N] * w)) ** 2
    f = np.fft.rfftfreq(N, 1.0 / 384000.0)
    return np.array([acc[(f >= lo) & (f < hi)].mean()
                     for lo, hi in zip(edges[:-1], edges[1:])]), f

edges = np.array([20, 45, 70, 90, 120, 180, 300, 600, 1200, 3000, 8000, 20000], float)
pd, _ = band(d, edges)
pb, _ = band(b, edges)
print("\nband            residual_dB   signal_dB   residual-signal")
rd = pd[:3].mean()
rb = pb[:3].mean()
for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
    dd = 10 * np.log10(pd[i] / rd)
    db = 10 * np.log10(pb[i] / rb)
    print(f"{lo:6.0f}-{hi:<6.0f} {dd:12.2f} {db:11.2f} {dd-db:13.2f}")

# slope of the residual across decades: low-pass => state injection
lo_b = pd[:2].mean()
hi_b = pd[8:].mean()
slope_db = 10 * np.log10(lo_b / hi_b) if hi_b > 0 else float("inf")
print(f"\nresidual bass-vs-treble: {slope_db:+.1f} dB")
print("  ~  0 dB  -> WHITE: dither or an independent output rounding stage.")
print("  > +12 dB -> LOW-PASS: the error is inside the recursion, i.e. a 1-ULP")
print("               state-rounding difference between the VPU asm and my port")
print("               (pole |z|~0.9987 amplifies 1 LSB by ~780x). The vendor")
print("               Python model cannot arbitrate this -- diff biquad.S.")
