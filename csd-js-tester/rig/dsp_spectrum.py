#!/usr/bin/env python3
"""Does the capture carry the bass shelf? No device needed.

The preset-2 curve is flat to ~90 Hz and 6 dB down above it, so the
spectrum of the capture says which of three things happened, without ever
agreeing a single frame:

  flat broadband          -> the DSP did NOT run (byte-exact bypass): the
                             capture is the unfiltered schedule, and the
                             anchor's failure is a formatting/alignment bug
  -6 dB step at ~90 Hz    -> the DSP DID run; the twin or the alignment is
                             wrong, and the mismatch is ours to chase
  neither                 -> the capture is not our stream at all (wrong
                             tap, mic path, stale arming)

Band power is measured as |X| mean-square over log-spaced bands from an
overlap-add windowed FFT, which is insensitive to the unknown offset --
power spectra do not care where the record starts.
"""
import numpy as np

FS = 384000.0
cap = np.load("/tmp/dsp_cap.npy")
cL, cR = cap[0], cap[1]
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")   # filtered twin
eL = exp[0]

# ---- 0. low-bit structure: does the capture even HAVE low bits? ----------
# The I2S loopback is 32-bit on paper. If the ADC leg only clocks 24 (or the
# wire drops its low byte twice on the way back), every captured 24-bit field
# is a multiple of 256: content still looks alive, but the word-exact anchor
# matches ~1/2^8 of frames -- which is exactly the 1/2048 seen at the anchor.
for name, arr in (("capture L", cL), ("capture R", cR),
                  ("filtered twin L", eL)):
    a = arr[:1 << 20].astype(np.uint32) & np.uint32(0xFFFFFF)
    low8 = (a & np.uint32(0xFF))
    z = int(np.count_nonzero(low8 == 0))
    print(f"{name}: low byte == 0 in {z}/{len(a)} "
          f"({'TRUNCATED to 24-bit' if z > 0.99*len(a) else 'low bits live'})")

def to24(a):
    """Signed 24-bit view of the wire word (unpack_frames does >>8)."""
    u = a.astype(np.uint32) >> np.uint32(8)
    u = u & np.uint32(0xFFFFFF)
    return np.where(u >= (1 << 23), u.astype(np.int64) - (1 << 24),
                    u.astype(np.int64))

def band_power(x, edges):
    """Mean-square spectral density per band, Welch with Hann windows."""
    N = 1 << 15
    n_seg = len(x) // N
    if n_seg < 4:
        raise SystemExit(f"need >=4 segments, have {n_seg}")
    w = np.hanning(N).astype(np.float64)
    acc = np.zeros(N // 2 + 1)          # rfft of N real -> N//2+1 bins
    for s in range(n_seg):
        seg = to24(x[s * N:(s + 1) * N]).astype(np.float64) * w
        acc += np.abs(np.fft.rfft(seg)) ** 2
    freqs = np.fft.rfftfreq(N, 1.0 / FS)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (freqs >= lo) & (freqs < hi)
        out.append(acc[m].mean() if m.any() else np.nan)
    return np.array(out), freqs

# decade-ish bands straddling the 90 Hz corner, log spaced
edges = np.array([20, 45, 70, 90, 120, 180, 300, 600, 1200, 3000, 8000, 20000],
                 float)
for name, arr in (("capture L", cL), ("capture R", cR),
                  ("filtered twin L", eL)):
    try:
        p, f = band_power(arr[:1 << 24], edges)
    except SystemExit as e:
        print(f"{name}: {e}")
        continue
    ref = p[:3].mean()                      # bass reference (flat region)
    db = 10 * np.log10(p / ref, where=p > 0)
    print(f"\n{name}  (band mean-square, dB relative to <70 Hz)")
    for (lo, hi), d in zip(zip(edges[:-1], edges[1:]), db):
        print(f"   {lo:7.0f}-{hi:<6.0f} Hz  {d:6.2f} dB")
    treble = p[3:].mean()
    print(f"   >> mean treble (>90 Hz) vs bass: {10*np.log10(treble/ref):6.2f} dB"
          "   (preset 2 expects about -6; bypass expects 0)")
