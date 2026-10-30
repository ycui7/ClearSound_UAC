#!/usr/bin/env python3
"""Which I2S slot does each USB channel land in -- read digitally, no mic.

The acoustic rig proved a stable, reproducible inversion: a tone written into
USB left is heard by the right capsule at 60-62 dB isolation, identically on
presets 0, 1 and 2, with the no-stream floor holding at -69.3 dBFS across all
three (0.1 dB spread). Preset-independent, so the DSP filter is not doing it.

That leaves two candidate locations, and the split matters because only one of
them is fixable in code:

  (a) USB channel -> I2S slot assignment (firmware: audiohub routing, or the
      host's channel map). Fix in code.
  (b) codec output -> analog trace -> capsule cavity. Fix in hardware.

The firmware answers this directly. Under CSD_DSP_DIAG_LOOP the audio thread
copies the samples it is about to hand to I2S onto the USB *in* path, so the
host can read, per channel, exactly what is headed for each I2S slot. That is a
digital comparison inside the firmware and cannot be affected by the analog
path, the cable, or the probe.

Read the rows as: play a tone into ONE USB channel, then look at which capture
channel carries it.

  tone into USB L -> capture ch0 loud : USB L is routed to I2S slot 0 (upstream
      is correct). Since the acoustic test says USB L is then HEARD by the right
      capsule, the inversion is downstream of the codec -- wiring or cavity.
  tone into USB L -> capture ch1 loud : USB L is being written into I2S slot 1.
      The inversion is upstream, in routing, and firmware can fix it.

Capture on this card is S32_LE only (S24_3LE is rejected by the driver). Both
streams run at once, which the board supports.
"""
import math
import os
import subprocess
import sys
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ears_matrix as m  # noqa: E402

DAC = m.find_dac_card(None)
# Resolve the app root from this file, not an absolute path: the tree
# lives inside the firmware repo (csd-js-tester/rig/ sits next to
# csd_diag.py) and the old scratch copy must not be baked in.
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = "/tmp/i2s_loop"
os.makedirs(TMP, exist_ok=True)
RATE, TONE, AMP = 48000, 1000.0, 0.3
print(f"DAC card {DAC}   tone {TONE:.0f} Hz at {AMP} FS")


def write_stim(path, al, ar, dur=3.0):
    """24-bit WAV stimulus; ffmpeg resamples to whatever the device wants."""
    n = int(RATE * dur)
    buf = bytearray()
    for i in range(n):
        t = i / RATE
        for a in (al, ar):
            v = int(round(a * (2 ** 23 - 1) * math.sin(2 * math.pi * TONE * t)))
            u = v & 0xFFFFFF
            buf += bytes((u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF))
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
        w.writeframes(bytes(buf))


def tone_level(wav):
    """Per-channel dBFS at the probe tone, from a S32_LE capture.

    Also reports whether the two capture channels are bit-identical. If the
    driver duplicates one channel into both (a channel-map artifact), the
    digital slot test is BLIND -- both slots would read the same whatever the
    firmware actually did -- and it must say so rather than print a match.
    """
    w = wave.open(wav)
    ch_n, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
    raw = w.readframes(w.getnframes())
    w.close()
    if sw != 4:
        raise SystemExit(f"{wav}: expected S32_LE capture, got {sw*8}-bit")
    v = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483647.0
    x = v.reshape(-1, ch_n)
    nper = 4096
    win = np.hanning(nper)
    out = []
    for ci in range(ch_n):
        ch = x[:, ci]
        ns = max((len(ch) - nper) // (nper // 2), 1)
        psd = np.zeros(nper // 2 + 1)
        for s in range(ns):
            seg = ch[s * (nper // 2): s * (nper // 2) + nper] * win
            psd += np.abs(np.fft.rfft(seg)) ** 2
        psd /= ns
        psd /= np.sum(win ** 2)
        psd[1:] *= 2
        db = 10 * np.log10(psd + 1e-30)
        freqs = np.fft.rfftfreq(nper, 1.0 / sr)

        def at(f):
            k = max(1, min(len(db) - 1, int(round(f / sr * nper))))
            return float(max(db[max(1, k - 1):k + 2]))
        mask = (freqs >= 2000) & (freqs <= 16000)
        mask &= ~((freqs > TONE - 200) & (freqs < TONE + 200))
        out.append((at(TONE), float(np.median(db[mask]))))
    dup = bool(ch_n >= 2 and np.array_equal(x[:, 0], x[:, 1]))
    zeros = bool(not np.any(v))
    return out, dup, zeros


def capture_play(stim, tag, secs=3):
    cap = f"{TMP}/{tag}.wav"
    if os.path.exists(cap):
        os.remove(cap)
    p = subprocess.Popen(["arecord", "-D", f"hw:{DAC},0", "-f", "S32_LE",
                          "-r", str(RATE), "-c", "2", "-d", str(secs),
                          "-t", "wav", cap],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    time.sleep(0.5)
    play = None
    if stim:
        play = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error",
                                 "-re", "-i", stim, "-f", "alsa", "-ac", "2",
                                 f"plughw:{DAC},0"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rc = p.wait()
    err = (p.stderr.read().decode(errors="replace") if p.stderr else "").strip()
    prc = play.wait() if play else 0
    perr = ""
    time.sleep(0.3)
    if rc != 0 or not os.path.exists(cap):
        print(f"    arecord rc={rc} {err[:160]}")
        return None, None, True
    if prc:
        print(f"    ffmpeg playback rc={prc} (device busy from a previous row? "
              "the capture is then measuring a stream that never carried samples)")
    return tone_level(cap)


print("\n=== sanity: is the LOOP stamp actually live? ===")
# If the build lacks CSD_DSP_DIAG_LOOP, the capture is just the ADC and every
# row will read near-silence; that must not be mistaken for "no swap".
rows, dup_flags, dead = {}, {}, []
for tag, al, ar in (("none", 0.0, 0.0), ("L", AMP, 0.0), ("R", 0.0, AMP),
                    ("both", AMP, AMP)):
    s = f"{TMP}/{tag}.wav"
    write_stim(s, al, ar)
    # One retry: the second and third rows came back all-zero in the first
    # attempt, which is a capture stream that never delivered, not a balanced
    # pair of I2S slots. A dead capture must be retried, not believed.
    for attempt in (1, 2):
        got = capture_play(None if tag == "none" else s, f"{tag}_{attempt}")
        if got[0] is not None and not got[2]:
            break
        time.sleep(1.0)
    if got[0] is None or got[2]:
        dead.append(tag)
        print(f"  USB {tag:4s} -> DEAD CAPTURE (all-zero buffer) -- unusable")
        rows[tag] = [(-300.0, -300.0), (-300.0, -300.0)]
        continue
    got, dup, _ = got
    rows[tag] = got
    dup_flags[tag] = dup
    parts = " ".join(f"ch{i} {lv:+6.1f}(fl {fl:+.1f})" for i, (lv, fl) in enumerate(got))
    # Built separately: an f-string cannot span the quote/brace nesting this
    # needed, and it parsed as an unterminated literal.
    dup_note = ("   <-- CHANNELS BIT-IDENTICAL: the driver duplicates one "
                "channel, so this test cannot distinguish slots" if dup else "")
    print(f"  USB {tag:4s} -> I2S-bound capture: {parts}{dup_note}")

usable = [t for t in rows if t not in dead]
baseline_rows = [t for t in usable if t == "none"] or usable
env = max(max(lv for lv, _ in rows[t]) for t in baseline_rows)
# Liveness = ANY stimulus row lifted well above the baseline. The first version
# tested only the "both" row, which happened to be a dead capture, so it called
# a working instrument dead while the "L" row two lines above showed a tone
# 25 dB over its floor.
live = any(max(lv for lv, _ in rows[t]) > env + 12
           for t in ("L", "R", "both") if t in usable)
print(f"\n  baseline: {env:+.1f} dBFS   tone present in loopback: "
      f"{'YES' if live else 'NO'}   dead captures: {dead or 'none'}")
# The path is blind only if NO row can tell the two capture channels apart.
# Testing dup per row and aborting on any hit was wrong: with nothing playing
# both channels hold the same idle pattern, so the "none" row is always
# bit-identical and aborted a run whose L and R rows differed by 51 dB.
discriminating = [t for t in ("L", "R") if t in usable
                  and max(lv for lv, _ in rows[t]) > env + 12
                  and abs(rows[t][0][0] - rows[t][1][0]) > 6.0]
if not discriminating:
    print("  ABORT: no stimulus row separates the two capture channels, so slot"
          " identity cannot be read through this path. Do not read a match here"
          " as correctness.")
    sys.exit(2)
print(f"  capture channels are independent (separated by rows: {discriminating})")

print("\n=== map ===")
verdicts = []
for src, other in (("L", "R"), ("R", "L")):
    lv0, _f0 = rows[src][0]
    lv1, _f1 = rows[src][1]
    loud = 0 if lv0 >= lv1 else 1
    iso = abs(lv0 - lv1)
    audible = max(lv0, lv1) > env + 12
    if not audible:
        verdicts.append((src, "NO-OUTPUT", iso))
        print(f"  USB {src} -> no audible content in the loopback")
        continue
    tag = "MATCHED" if loud == (0 if src == "L" else 1) else "INVERTED"
    verdicts.append((src, tag, iso))
    print(f"  USB {src} -> I2S slot {loud} (isolation {iso:.0f} dB)  {tag}")

allm = {t for _, t, _ in verdicts}
print()
if allm == {"MATCHED"}:
    print("  Upstream is CORRECT: USB L reaches I2S slot 0 and USB R slot 1.")
    print("  Since the capsules hear them swapped (rig/repeat_map.py, 4/4 reps,")
    print("  60-62 dB), the inversion is DOWNSTREAM of the codec -- analog")
    print("  channel pairing to the probe, or the codec's output routing.")
    print("  Firmware will not fix this; check the cable/board first.")
elif allm == {"INVERTED"}:
    print("  Upstream is INVERTED: USB L is written into I2S slot 1 and USB R")
    print("  into slot 0. That is a routing/channel-map defect in firmware")
    print("  (audiohub channel assignment / I2S slot order) and it IS fixable")
    print("  in code -- swap the slot assignment or the host channel map.")
else:
    print(f"  Mixed or unusable result {allm} -- do not conclude; repeat.")
