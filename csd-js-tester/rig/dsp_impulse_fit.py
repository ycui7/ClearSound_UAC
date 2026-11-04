#!/usr/bin/env python3
"""Decisive check: is the capture the twin, or the twin convolved with the
DAC's analog impulse response?

A loopback through the analog output cannot be sample-exact even when the DSP
is bit-perfect: the DAC's reconstruction filter and the analog output stage
have a finite impulse response, so the ADC sees a weighted sum of neighbouring
transmitted samples. If that is what we are looking at, the capture matches
sum_j h[j] * twin[k-j] for a short h -- and a pure "is this value in the set"
test would still score ~93% by chance, which is why set membership proved
nothing here.

Test: estimate the impulse response by least squares from the twin onto the
capture, over a stretch of kept frames. If capture ~= h * twin with residual
much smaller than the signal RMS, the loopback is a linear filtered copy and
bit-exactness is the wrong gate for it: the gate should be on the DAC's I2S
input side (already proven, preset 0) plus a frequency-response check.
If the residual stays large, the capture is not our stream at all.
"""
import numpy as np

NTAP = 33
NFR = 40_000

cap = np.load("/tmp/dsp_cap.npy")
cL = cap[0].astype(np.uint32)
cR = cap[1].astype(np.uint32)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
fL = (exp[0].astype(np.uint32) >> np.uint32(8))


def s24(u):
    u = u & np.uint32(0xFFFFFF)
    return np.where(u >= (1 << 23), u.astype(np.float64) - (1 << 24),
                    u.astype(np.float64))


real = ~((cL == 0) & (cR == 0))
idx = np.flatnonzero(real)
# contiguous runs of >= NTAP+8 kept frames, so the convolution window is clean
runs = []
st = idx[0]
prev = idx[0]
for v in idx[1:]:
    if v != prev + 1:
        if prev - st >= NTAP + 8:
            runs.append((st, prev - st + 1))
        st = v
    prev = v
if prev - st >= NTAP + 8:
    runs.append((st, prev - st + 1))
print(f"clean runs >= {NTAP+8}: {len(runs):,}")

# try a few plausible offsets: the capture stream is delayed by some unknown
# amount, so fit h for the best of a few candidate anchors
best = None
for anchor_cap, ln in runs[:40]:
    for off in (0, 48, 96, 576, 672):
        n = min(ln - NTAP, NFR)
        if n < 4000:
            continue
        y = s24(cL[anchor_cap + NTAP: anchor_cap + NTAP + n])
        A = np.zeros((n, NTAP))
        for j in range(NTAP):
            A[:, j] = s24(fL[off + NTAP - 1 - j: off + NTAP - 1 - j + n])
        h, res, rank, sv = np.linalg.lstsq(A, y, rcond=None)
        pred = A @ h
        rms_y = float(np.sqrt(np.mean(y ** 2)))
        rms_e = float(np.sqrt(np.mean((y - pred) ** 2)))
        gain = 20 * np.log10(max(rms_e, 1e-9) / max(rms_y, 1e-9))
        if best is None or gain < best[0]:
            best = (gain, anchor_cap, off, h, rms_y, rms_e)

gain, anchor_cap, off, h, rms_y, rms_e = best
print(f"best linear fit: anchor capture {anchor_cap:,}, schedule offset {off}")
print(f"  signal RMS {rms_y:,.0f}  residual RMS {rms_e:,.0f}  "
      f"({gain:.1f} dB: fit explains {-gain:.1f} dB of the signal)")
print("  impulse response taps (normalised):",
      ", ".join(f"{v/h[np.argmax(np.abs(h))]:+.3f}" for v in h))
print("\nread: residual <= -20 dB => capture is a linear filtered copy of the")
print("twin: the DSP is right and the analog loopback sets the accuracy floor.")
print("residual near 0 dB => the capture is not this stream.")
