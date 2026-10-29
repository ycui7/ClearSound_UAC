#!/usr/bin/env python3
"""Deterministic stimulus for the A3 latency gate.

13 unique linear chirps (band plan stepped per loop), 0.4 s silence between,
looped infinitely by ffmpeg. Unique-per-loop content means the
cross-correlation of two captures of the same looped stream has ONE peak
modulo the full 13-loop period (>> any plausible lag) -> absolute stream-phase
alignment between captures, even across firmware reloads.

Usage: mk_chirp.py out.wav
"""
import numpy as np, wave, sys

fs = 48000
T = 3.0
NLOOPS = 13

def chirp(f0, f1):
    t = np.arange(int(fs*T)) / fs
    return np.sin(2*np.pi*(f0*t + (f1-f0)/(2*T)*t**2)) * 0.5

sil = np.zeros(int(fs*0.4))
parts = []
for k in range(NLOOPS):
    f0 = 150.0 + 30.0*k
    f1 = 6000.0 + 900.0*k            # each loop: unique band plan
    parts.append(sil); parts.append(chirp(f0, f1))
x = np.concatenate(parts)
q = np.clip(x*(2**23-1), -2**23, 2**23-1).astype(np.int32)
b = np.zeros((len(q), 3), np.uint8)
b[:,0] = q & 0xff; b[:,1] = (q >> 8) & 0xff; b[:,2] = (q >> 16) & 0xff
st = np.repeat(b, 2, axis=1)
out = sys.argv[1] if len(sys.argv) > 1 else '/home/ycui/.hermes/cache/scratch/chirp_loop.wav'
w = wave.open(out, 'wb'); w.setnchannels(2); w.setsampwidth(3); w.setframerate(fs)
w.writeframes(st.tobytes()); w.close()
print(f"wrote {out}: {len(q)/fs:.2f} s ({NLOOPS} unique loops)")
