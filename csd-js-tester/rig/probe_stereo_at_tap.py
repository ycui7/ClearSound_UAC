#!/usr/bin/env python3
"""Is the device really stereo? Play a tone on ONE channel only and read both
captured channels separately.

The pairing probe found captured L == captured R on 100.00% of frames while the
stimulus has L and R uncorrelated by construction (noise24_vec offsets the seed
by 7919*ch, checked in source). Either the host stream is collapsed to one word
before the I2S output -- which would be a product defect, stereo shipping as
mono -- or the tap collapses it, which would only invalidate the instrument.

Decide it with a stimulus that cannot be ambiguous: a 1 kHz tone on ONE channel
and digital silence on the other. Silence is unambiguous because the DSP is
pinned to preset 0 and the codec is unmuted at 0 dB, so a silent input must
produce a silent output.

  tone on L, R silent : expect captured L = tone, captured R = silent
  tone on R, L silent : expect captured R = tone, captured L = silent
  both silent         : expect both silent  (the control: proves silence is
                        measurable, so a non-silent "silent" leg means something)

If the silent channel comes back carrying the tone, the channels are summed or
one output mirrors the other upstream of the tap. Report which side.

Amplitude is peak |word - mid|, and pitch is the FFT peak, so a tone and a
stale constant are never confused (a constant has no peak).
"""
import importlib.util
import os
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import csd_signal as sig                                  # noqa: E402

RATE = sig.RATE
N = 4096


def load_gate():
    spec = importlib.util.spec_from_file_location(
        "g", os.path.join(HERE, "csd_bitperfect_gate.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def stream_pattern(g, sched, seconds):
    """Play a caller-built schedule; capture both channels."""
    payload_addr = sched.ctypes.data
    total = len(sched)
    play, pbuf = g.openp(0, g.PLAY_BUFSZ)
    cap, cbuf = g.openp(1, g.BUFSZ)
    g.alsa.snd_pcm_start(play)
    g.alsa.snd_pcm_start(cap)
    stop = threading.Event()
    st = {"written": 0, "fatal": 0}

    def writer():
        n = 0
        while not stop.is_set() and n < total:
            k = min(16384, total - n)
            off = 0
            while off < k and not stop.is_set():
                r = g.alsa.snd_pcm_writei(play, payload_addr + (n + off) * 8,
                                          k - off)
                if r < 0:
                    if g.alsa.snd_pcm_recover(play, r, 1) < 0:
                        st["fatal"] = r
                        return
                    g.alsa.snd_pcm_start(play)
                    continue
                off += r
                n += r
                st["written"] = n

    tw = threading.Thread(target=writer, daemon=True)
    tw.start()
    time.sleep(0.5)
    buf = (ctypes_char := (g.ctypes.c_char * (N * 8)))()
    got = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        r = g.alsa.snd_pcm_readi(cap, buf, N)
        if r <= 0:
            g.alsa.snd_pcm_recover(cap, r if r < 0 else -1, 1)
            g.alsa.snd_pcm_start(cap)
            continue
        got.append(bytes(buf[:r * 8]))
    stop.set()
    tw.join(timeout=2)
    for p in (play, cap):
        g.alsa.snd_pcm_drop(p)
        g.alsa.snd_pcm_close(p)
    if not got:
        return None, None, st
    f = np.concatenate([np.frombuffer(c, dtype="<u4") for c in got])
    return ((f[0::2] >> np.uint32(8)).astype(np.int64),
            (f[1::2] >> np.uint32(8)).astype(np.int64), st)


def describe(name, w):
    """Peak deviation from mid and FFT pitch of one captured channel."""
    if w is None or len(w) == 0:
        return f"{name}: NO DATA"
    mid = sig.M24 >> 1
    dev = np.abs(w - mid)
    nz = float((w != 0).mean())
    peak = int(dev.max()) if len(dev) else 0
    # pitch only meaningful if there is real AC energy
    x = (w - w.mean()).astype(np.float64)
    if not np.any(x):
        pitch = 0.0
    else:
        X = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        pitch = int(np.argmax(X)) * RATE / len(x)
    const = len(np.unique(w[w != 0])) <= 3
    return (f"{name}: nonzero {nz:.4f}  peak|mid| {peak:>7,}  "
            f"pitch {pitch:>8.1f} Hz  distinct {len(np.unique(w[w != 0])):,}"
            + ("  [CONSTANT]" if const else ""))


def build(tones):
    """tones = (l_tone, r_tone): each True plays a 1 kHz sine, False is zeros."""
    n = int(2.5 * RATE)
    idx = np.arange(n, dtype=np.uint64)
    s = np.zeros((n, 2), dtype="<u4")
    if tones[0]:
        s[:, 0] = sig.tone24_vec(idx, freq=1000.0, ch=0).astype(np.uint32) << np.uint32(8)
    if tones[1]:
        s[:, 1] = sig.tone24_vec(idx, freq=1000.0, ch=1).astype(np.uint32) << np.uint32(8)
    return s


def main():
    g = load_gate()
    g.pin_path()
    print(f"\n  tap arm: {g.scpi('DEBUG:LOOPback 1')}")
    print(f"  clear  : {g.scpi('DEBUG:LOOPback:CLEAR')}")
    results = {}
    for label, tones in (("control: both silent ", (False, False)),
                         ("tone on L, R silent  ", (True, False)),
                         ("tone on R, L silent  ", (False, True))):
        L, R, st = stream_pattern(g, build(tones), 2.0)
        results[label] = (L, R)
        print(f"\n  --- {label}  (written {st['written']:,} frames) ---")
        print("   ", describe("captured L", L))
        print("   ", describe("captured R", R))
        if L is not None:
            print(f"    L == R on {100.0*float((L == R).mean()):.2f}% of frames")

    print("\n  VERDICT")
    cl, cr = results["control: both silent "]
    tl, tr = results["tone on L, R silent  "]
    rl, rr = results["tone on R, L silent  "]
    mid = sig.M24 >> 1

    def loud(w):
        return w is not None and len(w) and int(np.abs(w - mid).max()) > 32768

    def quiet(w):
        return w is not None and len(w) and int(np.abs(w - mid).max()) <= 1024

    problems = []
    if not (quiet(cl) and quiet(cr)):
        problems.append("CONTROL FAILED: both inputs silent but a capture "
                        "channel is not quiet -- the measurement is void and "
                        "nothing below can be credited")
    else:
        print("    control: silence in -> both capture channels quiet. OK")
    if loud(tl) and not quiet(tr):
        problems.append("tone on L appeared on captured R as well")
    if loud(rr) and not quiet(rl):
        problems.append("tone on R appeared on captured L as well")
    if not loud(tl):
        problems.append("tone on L did NOT appear on captured L")
    if not loud(rr):
        problems.append("tone on R did NOT appear on captured R")
    if not problems:
        print("    STEREO IS CORRECT at the tap: L-only reaches L, R-only "
              "reaches R, silence is silent. The earlier L==R must have come "
              "from the stimulus or the mix path with the generator armed, "
              "not from the tap.")
    for p in problems:
        print(f"    !! {p}")


if __name__ == "__main__":
    main()
