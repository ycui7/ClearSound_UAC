#!/usr/bin/env python3
"""Cross-correlate two mono captures, return integer sample offset (b vs a)."""
import sys, numpy as np, wave

def load(path):
    w = wave.open(path); assert w.getnchannels() == 2, "stereo expected"
    n = w.getnframes(); raw = w.readframes(n)
    # 24-bit LE interleaved
    x = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
    v = (x[:,0].astype(np.int32) | (x[:,1].astype(np.int32) << 8)
         | (x[:,2].astype(np.int32) << 16))
    v = np.where(v >= 2**23, v - 2**24, v).astype(np.float64)
    return v

def corr_offset(a, b, max_lag=4800):
    a = a - a.mean(); b = b - b.mean()
    a = a / (np.sqrt(np.sum(a*a))+1e-9); b = b / (np.sqrt(np.sum(b*b))+1e-9)
    # FFT cross-correlation, search |lag| <= max_lag
    N = 1 << (len(a)+len(b)).bit_length()
    A = np.fft.rfft(a, N); B = np.fft.rfft(b, N)
    cc = np.fft.irfft(B * np.conj(A), N)
    lags = np.arange(-max_lag, max_lag+1)
    vals = np.array([cc[l % N] for l in lags])
    k = int(np.argmax(vals))
    return int(lags[k]), float(vals[k])

if __name__ == "__main__":
    a = load(sys.argv[1]); b = load(sys.argv[2])
    ch = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    la, _ = corr_offset(a[0::2][:1<<19], b[0::2][:1<<19]) if ch == 0 else (0,0)
    if ch == 1:
        la, _ = corr_offset(a[1::2][:1<<19], b[1::2][:1<<19])
    print(la)
