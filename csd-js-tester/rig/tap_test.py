#!/usr/bin/env python3
"""Establish which probe channel is the physical LEFT capsule.

Everything in this investigation so far is a relationship between the host's
channel numbering and the probe's channel numbering:

  host/ALSA channel 0 -> I2S slot 0 -> codec CH1 -> DAC1 output -> probe ch1
  host/ALSA channel 1 -> I2S slot 1 -> codec CH2 -> DAC2 output -> probe ch0

That is self-consistent and reproducible (60-62 dB isolation, 4/4 reps, all
presets, verified against the codec's own register reads). But it has never
been anchored to physical left and right, because the probe's channel order was
assumed. If the probe's cable or its ALSA channel order is mirrored relative to
its capsules, every reading is correct and nothing is swapped -- the labels
would be.

So this anchors it physically. Tap one capsule and see which capture channel
jumps. Ask the user which capsule they tapped, and the mapping is settled with
no instrumentation to trust: a tap is broadband, huge and unmistakable against
a -69 dBFS floor.

Run it twice, once per capsule. A channel that responds to a tap on the LEFT
capsule is the left channel. If probe ch0 is the left capsule, then the chain
above is a genuine L/R inversion at the board's analog output. If probe ch1 is
the left capsule, the "inversion" was a labeling artifact and the DAC is wired
correctly.

Keep the DAC quiet during this -- a running stream adds 1 kHz energy that makes
the tap harder to read, not easier.
"""
import os
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ears_matrix as m  # noqa: E402

EARSC = m.find_ears_card(None)
SECS = int(os.environ.get("TAP_SECS", "8"))
print(f"probe card {EARSC}, {SECS} s per capture")
print("Keep the DAC silent. Put a finger on ONE capsule and tap it firmly and")
print("repeatedly -- about twice a second -- for the whole capture, and tell me")
print("which capsule (as you face the probe) you tapped.\n")

m.ensure_capture_switch(EARSC)


def capture(path):
    if os.path.exists(path):
        os.remove(path)
    r = subprocess.run(["arecord", "-D", f"hw:{EARSC},0", "-f", "S24_3LE",
                        "-r", "48000", "-c", "2", "-d", str(SECS),
                        "-t", "wav", path],
                       capture_output=True, timeout=SECS + 30)
    if r.returncode:
        raise SystemExit(f"arecord failed: {r.stderr.decode()[:200]}")
    return path


def tap_energy(path):
    """Peak envelope per channel, dBFS: a tap is a broadband transient."""
    import wave
    w = wave.open(path)
    sw, sr, chn = w.getsampwidth(), w.getframerate(), w.getnchannels()
    raw = w.readframes(w.getnframes())
    w.close()
    a = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.uint32)
    v = (a[:, 0] | (a[:, 1] << 8) | (a[:, 2] << 16)).astype(np.int32)
    v[v >= 1 << 23] -= 1 << 24
    x = v.reshape(-1, chn) / float(1 << 23)
    out = []
    for ci in range(chn):
        ch = x[:, ci]
        # short-window peak: 20 ms
        win, n = int(0.02 * sr), len(ch)
        peaks = [np.max(np.abs(ch[s:s + win])) for s in range(0, n - win, win)]
        peaks = [p for p in peaks if p > 0]
        out.append((20 * np.log10(max(np.max(peaks), 1e-9)),
                    20 * np.log10(max(np.median(peaks), 1e-9))))
    return out


print("  (get ready -- tapping starts now)")
path = capture(f"/tmp/tap_{int(time.time())}.wav")
pk = tap_energy(path)
for ci, (peak, med) in enumerate(pk):
    print(f"  probe ch{ci}: peak {peak:+6.1f} dBFS   median-window {med:+6.1f} dBFS")

loud = 0 if pk[0][0] > pk[1][0] else 1
iso = abs(pk[0][0] - pk[1][0])
print(f"\n  the tap landed on probe ch{loud} (separation {iso:.0f} dB)")
if iso < 10:
    print("  Separation is weak -- tap harder, or the other capsule, and rerun."
          " A tap must be obvious against the -69 dBFS floor before it can"
          " settle channel identity.")
    sys.exit(2)
print("\n  Now: which capsule did you tap, as you face the probe?")
print("    - if you tapped the LEFT capsule and it lit probe ch1, then probe")
print("      ch1 IS left, and the chain 'host ch0 -> DAC1 -> probe ch1' means")
print("      the board's left output is correct -- no inversion.")
print("    - if you tapped the LEFT capsule and it lit probe ch0, then probe")
print("      ch0 is left and the chain above IS a real L/R inversion at the")
print("      analog output: the codec's DAC1 drives the right ear.")
