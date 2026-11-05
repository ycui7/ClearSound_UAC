#!/usr/bin/env python3
"""What is the built-in mic path actually returning?

Case 11 calls it "a repeating stamp pattern, not a mic" on two numbers: spectral
flatness 0.34 and autocorrelation 0.93. With a REGISTER-LESS mic ADC (owner,
2026-11-05: no control registers, it streams as soon as MCLK is up) neither
number proves anything on its own -- a bench microphone next to a switching PSU
also has a dominant spectral line and a periodic component, and a 24-bit sample
landed in the wrong bit position looks like a very quiet signal, not a broken
one. So look at the data before believing the verdict.

Prints, per channel: rms/peak, the top spectral peaks WITH frequencies (mains and
switching noise are identifiable by where they sit; a counter is not), the
longest exact run of repeated samples and whether repeats form an arithmetic
progression (a counter) or not (acoustic), and the same statistics after a
bit-reversal -- because the TX path bit-reverses for the DAC, and if the RX path
inherits that while the ADC is MSB-first, every sample is in the wrong place.
"""
import os
import subprocess
import sys

import numpy as np

sys.path.insert(0, ".")
from rig.csd_suite import cards  # noqa: E402

import os as _os
RATE = int(_os.environ.get("MIC_RATE", "48000"))   # the capture stream shares the
SECS = 2.0                                          # playback clock: see findings
dac = [c for c, n in cards().items() if n and "DACUAC20" in n][0]


def rec(ch):
    """Capture using EXACTLY the invocation case 11 uses (stereo, wav, 48k).

    An earlier draft asked for -c 1 and got zero bytes, which looked like "no
    capture data" and was actually the probe asking for a format the stream does
    not offer -- the same class of error as a firmware verdict from a bad read.
    Reuse the known-good invocation so the only difference here is the analysis.
    """
    import wave
    path = "/tmp/mic_probe.wav"
    if os.path.exists(path):
        os.remove(path)
    subprocess.run(["arecord", "-q", "-D", f"hw:{dac},0", "-f", "S32_LE",
                    "-r", str(RATE), "-c", "2", "-d", str(int(SECS)),
                    "-t", "wav", path], capture_output=True)
    if not os.path.exists(path):
        return np.zeros(0, dtype=np.int32)
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        raw = np.frombuffer(w.readframes(n), dtype="<i4").reshape(n, 2)
    return raw[:, ch]


def bitrev32(a):
    u = (a.astype(np.uint32) & 0xFFFFFFFF)
    out = np.zeros_like(u)
    for b in range(32):
        out |= ((u >> b) & np.uint32(1)).astype(np.uint32) << (31 - b)
    return out.astype(np.int64)


def stats(a, tag):
    if a.size < 1024:
        print(f"  {tag}: too few samples ({a.size})")
        return
    x = a.astype(np.float64)
    rms = np.sqrt(np.mean(x ** 2))
    pk = np.max(np.abs(x))
    sp = np.abs(np.fft.rfft(x - x.mean()))
    fr = np.fft.rfftfreq(x.size, 1.0 / RATE)
    top = np.argsort(sp)[-5:][::-1]
    peaks = ", ".join(f"{fr[i]:.0f}Hz" for i in top)
    # longest exact repeat run
    d = np.diff(x)
    zero_run = 0
    run = 0
    best = 0
    for v in d:
        if v == 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    # distinct values vs span: a counter walks monotonically, a mic does not
    mono = int(np.sum(d > 0))
    print(f"  {tag}: rms={20*np.log10(max(rms,1e-9)/2**31):7.1f} dBFS "
          f"peak={20*np.log10(max(pk,1)/2**31):7.1f} "
          f"distinct={len(np.unique(x)):6d}/{x.size} "
          f"maxexactrepeat={best:5d} upward_steps={mono:6d} "
          f"top_peaks=[{peaks}]")


for ch in (0, 1):
    a = rec(ch)
    print(f"-- capture channel {ch} (n={a.size}) --")
    stats(a, "as-is ")
    stats(bitrev32(a), "bitrev")
