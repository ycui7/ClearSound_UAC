#!/usr/bin/env python3
"""FFT analysis of an EARS stereo recording.

Reports per-channel: DC, low-freq hum bins, noise floor (median PSD over
2-16 kHz excluding tones), peak bin in 20 Hz-20 kHz, SNR vs floor for each
probe tone given on the command line.

Usage: ears_fft.py <file.wav> [tone1 tone2 ...]
"""
import sys
import wave

import numpy as np

path = sys.argv[1]
tones = [float(t) for t in sys.argv[2:]] or [440.0, 1000.0]

w = wave.open(path)
assert w.getnchannels() == 2 and w.getsampwidth() == 3, "need stereo S24_3LE"
raw = w.readframes(w.getnframes())
sr = w.getframerate()
w.close()

a = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.uint32)
v = (a[:, 0] | (a[:, 1] << 8) | (a[:, 2] << 16)).astype(np.int32)
v[v >= 1 << 23] -= 1 << 24
x = (v.reshape(-1, 2) / 8388608.0)   # stereo float, ±1 FS

nper = 4096
win = np.hanning(nper)
for ci, nm in enumerate("LR"):
    ch = x[:, ci]
    # Welch PSD (one-sided), dB relative to full-scale sine
    ns = (len(ch) - nper) // (nper // 2)
    psd = np.zeros(nper // 2 + 1)
    for s in range(ns):
        seg = ch[s * (nper // 2): s * (nper // 2) + nper] * win
        f = np.fft.rfft(seg)
        psd += np.abs(f) ** 2
    psd /= max(ns, 1)
    psd /= (np.sum(win ** 2))           # normalize
    psd[1:] *= 2                        # one-sided
    db = 10 * np.log10(psd + 1e-30)
    freqs = np.fft.rfftfreq(nper, 1 / sr)

    def bin_at(f0):
        k = int(round(f0 / sr * nper))
        k = max(1, min(len(db) - 1, k))
        return max(db[max(1, k - 1):k + 2])

    floor_mask = (freqs >= 2000) & (freqs <= 16000)
    for t in tones:
        floor_mask &= ~((freqs > t - 200) & (freqs < t + 200))
    floor = np.median(db[floor_mask])

    band = (freqs >= 20) & (freqs <= 20000)
    pk = freqs[band][np.argmax(db[band])]
    print(f"mic{nm}: DC={db[0]:+.1f}dBFS  floor(2-16k)={floor:+.1f}dBFS/blk "
          f"peak={pk:.0f}Hz@{db[band].max():+.1f}dBFS")
    for t in tones:
        b = bin_at(t)
        print(f"   {t:>7.0f}Hz: {b:+.1f}dBFS  SNR={b - floor:+.1f}dB")
    hum = bin_at(60)
    hum2 = bin_at(120)
    print(f"   hum50/60={bin_at(50):+.1f} {hum:+.1}  120Hz={hum2:+.1}dBFS")
