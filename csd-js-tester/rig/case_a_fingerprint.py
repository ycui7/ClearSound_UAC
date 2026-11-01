#!/usr/bin/env python3
"""What IS the case-A artifact? Spectral fingerprint, not the 1 kHz bin.

The gate reads one bin (1 kHz) and calls anything above floor+6 a tone. That
made every decision so far, but we have never looked at the whole spectrum of
the artifact: its exact frequency, its sidebands, whether it is one coherent
line or broadband hash. The fingerprint discriminates the surviving suspects
in a single cold boot:

  exactly 1000.0 Hz, one line, stable across 1 s windows
      -> an NCO/sine: something ARMED the generator (it boots all-zero/OFF,
         so this would mean a boot-path arm we have not found)
  lines at multiples/half-rate patterns (e.g. 12 k, 6 k, or a 2-sample
         periodic buzz) -> the DSD underflow word 0x96969696 reaching I2S as
         PCM payload (a periodic bit pattern IS a tone)
  broad comb, peaks at clock-derived spots (e.g. 24.576/12.288 MHz /n, or
         ~48 k/n) -> PLL / switching artifact on the analog side
  noise rise with no lines -> something thermal/rail, not digital content

Legs, one cold boot: A0 (no stream ever), FIRST zero stream, SECOND zero
stream, plus a generator 1 kHz leg as the coherent-tone reference.

Usage: python3 rig/case_a_fingerprint.py
"""
from __future__ import annotations
import os, subprocess, sys, time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ears_matrix as EM

APP = os.path.dirname(HERE)
PY = f"{APP}/.venv/bin/python"
CAP_D = 4


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def load_wav(path):
    import wave
    w = wave.open(path)
    ch_n, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
    raw = w.readframes(w.getnframes()); w.close()
    a = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.uint32)
    v = (a[:, 0] | (a[:, 1] << 8) | (a[:, 2] << 16)).astype(np.int32)
    v[v >= 1 << 23] -= 1 << 24
    return v.reshape(-1, 2) / EM.FULL, sr


def spectrum(x, sr, nper=16384):
    """Welch PSD, dBFS. nper 16384 -> ~2.9 Hz bins: enough to say 'exactly
    1000.0' vs 'near but not'."""
    win = np.hanning(nper)
    ns = max((len(x) - nper) // (nper // 2), 1)
    psd = np.zeros(nper // 2 + 1)
    for s in range(ns):
        seg = x[s * (nper // 2): s * (nper // 2) + nper] * win
        psd += np.abs(np.fft.rfft(seg)) ** 2
    psd /= ns
    psd /= np.sum(win ** 2)
    psd[1:] *= 2
    db = 10 * np.log10(psd + 1e-30)
    return db, np.fft.rfftfreq(nper, 1.0 / sr)


def peaks(db, freqs, n=8, floor_db=-90.0):
    """Local maxima above floor, strongest first, 200 Hz apart."""
    idx = []
    order = np.argsort(db)[::-1]
    for k in order:
        if k < 2 or k >= len(db) - 2 or db[k] < floor_db:
            continue
        if db[k] < db[k - 1] or db[k] < db[k + 1]:
            continue
        f = freqs[k]
        if all(abs(f - freqs[j]) > 200 for j in idx):
            idx.append(int(k))
        if len(idx) >= n:
            break
    return [(float(freqs[k]), float(db[k])) for k in idx]


def coherence(path, freq=1000.0):
    """Amplitude of ONE specified bin in three consecutive 1 s windows. A
    coherent tone from a free-running NCO keeps constant amplitude; random
    or hashed content wanders by several dB.

    The bin MUST be named. An earlier version argmaxed the spectrum -- which
    locks onto the 29.3 Hz bench line that sits at ~-3 dBFS in EVERY leg
    (A0, artifact, reference: mic diaphragm / room, not ours). Argmax then
    'measured the room' and produced a false incoherent verdict for the
    artifact. Coherence is only meaningful at the artifact's own frequency,
    which for case A is the rig tone at 1 kHz."""
    x, sr = load_wav(path)
    n = len(x)
    seg = n // 3
    vals = []
    for i in range(3):
        db, fr = spectrum(x[i * seg:(i + 1) * seg, 0], sr)
        k = int(round(freq / sr * len(db)))
        k = max(1, min(len(db) - 2, k))
        vals.append(float(max(db[k - 1:k + 2])))
    return vals


def leg(tag, ff_args=None, secs=CAP_D):
    cap = os.path.join(TMP, f"{tag}.wav")
    EM.kill_orphan_arecords()
    p = subprocess.Popen(
        ["arecord", "-D", f"hw:{EARS},0", "-f", "S24_3LE", "-r", "48000",
         "-c", "2", "-d", str(secs), "-t", "wav", cap],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    ff = None
    if ff_args:
        ff = subprocess.Popen(ff_args, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    p.wait()
    if ff:
        ff.terminate(); ff.wait(timeout=10)
    x, sr = load_wav(cap)
    out = {}
    for ci, nm in enumerate("LR"):
        db, fr = spectrum(x[:, ci], sr)
        out[nm] = (peaks(db, fr), float(np.median(db[(fr >= 2000) & (fr <= 16000)])))
    print(f"\n  {tag}:")
    for nm in "LR":
        pk, fl = out[nm]
        tops = "  ".join(f"{f:8.1f}Hz@{d:+6.1f}" for f, d in pk[:5])
        print(f"    [{nm}] floor {fl:6.1f}  tops: {tops}")
    return cap, out


def peak_at(path, freq):
    """dBFS of the strongest bin within +/-15 Hz of `freq`, L channel."""
    x, sr = load_wav(path)
    db, fr = spectrum(x[:, 0], sr)
    k = int(round(freq / sr * len(db)))
    k = max(1, min(len(db) - 2, k))
    return float(max(db[k - 1:k + 2]))


def main():
    global EARS, DAC, TMP
    idn = scpi("*IDN?")
    if "ClearSound" not in idn:
        print(f"ABORT: firmware is not ours ({idn!r})"); return 1
    print(f"LIVE BUILD: {idn}")
    DAC = EM.find_dac_card(None)
    EARS = EM.find_ears_card(None)
    TMP = EM.tempfile.mkdtemp(prefix="fprint_")
    EM.ensure_capture_switch(EARS)
    EM.set_volume(DAC, EM.VOL_0DB, EM.VOL_0DB, TMP)

    print("\n== A0: never streamed ==")
    cap0, r0 = leg("a0")
    Z = os.path.join(TMP, "z.wav"); EM.write_tone24(Z, 20.0, 0.0, 0.0)
    zeros = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop",
             "-1", "-i", Z, "-f", "alsa", "-ac", "2", f"plughw:{DAC},0"]

    print("\n== FIRST zero stream ==")
    cap1, r1 = leg("first", zeros)
    time.sleep(1.0)
    print("\n== SECOND zero stream ==")
    cap2, r2 = leg("second", zeros)

    # Coherent reference: generator 1 kHz so 'what a real NCO line looks like'
    # is in the same boot, same everything.
    s = scpi("GENerator:SINe 48000,1000000,1073741824,0")
    print(f"\n== REFERENCE leg: generator 1 kHz at full scale ({s or 'ok'}) ==")
    cap3, r3 = leg("ref-tone", zeros)
    scpi("GENerator:OFF")

    print("\n== coherence AT 1 kHz (3 x 1s windows, L) ==")
    coh = {}
    for tag, cap in (("a0", cap0), ("first", cap1), ("second", cap2),
                     ("ref-tone", cap3)):
        v = coherence(cap, 1000.0)
        coh[tag] = v
        print(f"  {tag:9s} {v[0]:+6.1f} {v[1]:+6.1f} {v[2]:+6.1f}   "
              f"spread {max(v) - min(v):4.1f} dB")

    print("\n== read ==")
    # Judge the ARTIFACT at the rig tone (1 kHz), not at argmax: the 29.3 Hz
    # bench line dominates the raw spectrum in every leg and hijacked the
    # verdict last time. Coherence at 1 kHz is the discriminator that
    # separates an armed NCO (constant bin, like the reference leg) from
    # hashed/prefill content (wandering bin).
    f0a, d0 = (r0["L"][0][0] if r0["L"][0] else (0.0, -200.0))
    f1, d1 = (r1["L"][0][0] if r1["L"][0] else (0.0, -200.0))
    v1 = coh["first"]
    tone1 = peak_at(cap1, 1000.0)
    tone0 = peak_at(cap0, 1000.0)
    tone2 = peak_at(cap2, 1000.0)
    toneref = peak_at(cap3, 1000.0)
    print(f"  1 kHz bin:  a0 {tone0:+.1f}   first {tone1:+.1f}   "
          f"second {tone2:+.1f}   ref-NCO {toneref:+.1f} dBFS")
    print(f"  first-stream coherence at 1 kHz: spread "
          f"{max(v1) - min(v1):.1f} dB "
          f"(reference NCO spread {max(coh['ref-tone']) - min(coh['ref-tone']):.1f} dB)")
    artifact = tone1 - max(tone0, tone2)
    if artifact < 6.0:
        print("  -> NO artifact this boot at 1 kHz -- consistent with the "
              "measured intermittency of case A. Re-run N boots; do not "
              "attribute a quiet boot.")
        return 2
    spread = max(v1) - min(v1)
    if abs(tone1 - toneref) < 6.0 and spread < 3.0:
        print("  -> COHERENT line at 1 kHz: an armed NCO-like source. The "
              "generator boots OFF (mode 0), so hunt a boot-path arm.")
    elif spread >= 3.0:
        print("  -> INCOHERENT at its own frequency: prefill/underflow-word "
              "content or analog transient, not an armed generator. Pair "
              "with MIXer:DACPeak? at 7 nodes on the same boot.")
    else:
        print(f"  -> tone-like but level differs from reference by "
              f"{tone1 - toneref:+.1f} dB: single-shot burst (unmute ramp / "
              "chip transient).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
