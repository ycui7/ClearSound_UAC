#!/usr/bin/env python3
"""Bench-state discriminator for case A: WHERE does the -45 dBFS come from?

The two rigs disagree not on the DELTA but on the FLOOR:
  ears_matrix    A0 (no stream) 1kHz = -56 dBFS, zeros = -45   (+11 -> FAIL)
  case_a_timing  A0 (no stream) 1kHz = -92 dBFS, zeros = -84   (+7.6)
Same build, same bench, one hour apart. The deltas match; ears runs 36 dB
above a clean baseline. Something is sitting at ~-56 dBFS at 1 kHz in the
ears session and NOT in a fresh one. Prime suspect: an ALREADY-OPEN
playback substream -- when any process holds pcm0p open, the UAC2 alt
setting stays up and the board keeps streaming zeros forever, so "no
stream" A0 is actually "zeros + USB ticking + board active" and every
later reading shares that floor. That is ALSA/kernel state our rigs
create and never clear; a code bug we can see and fix.

Probe at each stage: /proc/asound/card<N>/pcm0p/sub0/{status,hw_params}
(state + owner pid), fuser on the device nodes, EARS 1kHz level. Sequence:
fresh baseline -> ears-style zeros case -> re-check "silent" -> tone case
-> re-check "silent". If the floor rises after the first playback AND
status shows RUNNING/prem with a pid, the holder is proven and killed
before measuring anything else.
"""
import glob
import math
import os
import re
import subprocess
import sys
import time
import wave

import numpy as np

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
RATE = 48000
TONE_HZ = 1000.0
SECS = 3


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def card_number(*pats):
    for line in open("/proc/asound/cards"):
        m = re.search(r"^\s*(\d+)\s*\[", line)
        if m and any(p in line.lower() for p in pats):
            return m.group(1)
    return None


XMOS = os.environ.get("CSD_XMOS_CARD") or card_number("xmos")
MIC = os.environ.get("CSD_MIC_CARD") or card_number("e36db", "e.a.r.s", "36db")
if not XMOS or not MIC:
    sys.exit("cards missing")
print(scpi("*IDN?"))

Z = os.path.join(os.environ.get("TMPDIR", "/tmp"), "bs_zeros.wav")
T = os.path.join(os.environ.get("TMPDIR", "/tmp"), "bs_tone.wav")


def stim(path, tone):
    n = int(RATE * (SECS + 4))
    buf = bytearray()
    for i in range(n):
        v = 0
        if tone:
            v = int(round(0.8 * (2 ** 23 - 1)
                          * math.sin(2 * math.pi * TONE_HZ * i / RATE)))
        u = v & 0xFFFFFF
        word = bytes((u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF))
        buf += word + word
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
        w.writeframes(bytes(buf))


stim(Z, False); stim(T, True)


def pcm_state(tag):
    print(f"  [{tag}] pcm state:")
    for sub in sorted(glob.glob(f"/proc/asound/card{XMOS}/pcm*p/sub0")):
        st = open(f"{sub}/status").read().strip().splitlines()
        try:
            hp = open(f"{sub}/hw_params").read().strip().splitlines()
        except OSError:
            hp = ["<busy/none>"]
        fmt = next((l for l in hp if l.startswith("format")), "")
        print(f"    {sub.split('/')[-2]}: {st[0] if st else '?'} | {fmt}")
    r = subprocess.run(["fuser", "-v", "/dev/snd/*"], capture_output=True,
                       text=True)
    lines = [l for l in (r.stdout or "").splitlines() if "pcm" in l.lower()]
    for l in lines[:6]:
        print(f"    holder: {l.strip()}")
    pg = subprocess.run(["pgrep", "-a", "ffmpeg"], capture_output=True,
                        text=True).stdout.strip()
    print(f"    ffmpeg procs: {pg or '(none)'}")


def ear_dbfs(tag):
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    raw = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"bs_{tag}.raw")
    if os.path.exists(raw):
        os.remove(raw)
    subprocess.run(["arecord", "-q", "-D", f"hw:{MIC},0", "-f", "S24_3LE",
                    "-r", str(RATE), "-c", "2", "-d", str(SECS), "-t", "raw",
                    raw], capture_output=True)
    b = np.frombuffer(open(raw, "rb").read(), dtype=np.uint8)
    b = b[:(b.size // 3) * 3].reshape(-1, 3)
    v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
         (b[:, 2].astype(np.int32) << 16))
    v = np.where(v & 0x800000, v - (1 << 24), v)
    out = {}
    for name, s in (("L", v[0::2]), ("R", v[1::2])):
        x = s.astype(np.float64)
        spec = np.abs(np.fft.rfft(x - x.mean())) * 2.0 / x.size
        k = int(round(TONE_HZ * x.size / RATE))
        pk = float(np.max(spec[max(1, k - 3):k + 4]))
        out[name] = 20 * math.log10(max(pk, 1e-12) / 2 ** 23)
    print(f"  [{tag}] EARS 1kHz L {out['L']:+.1f} R {out['R']:+.1f} dBFS")
    return out


def play(wav, secs):
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-t", str(secs), "-i", wav, "-f", "alsa", "-ac", "2",
         f"plughw:{XMOS},0"], capture_output=True, text=True)


print("\n-- 0. fresh board state --")
pcm_state("fresh")
base = ear_dbfs("fresh")

print("\n-- 1. ears-style zeros case (arecord first, then playback) --")
raw = os.path.join(os.environ.get("TMPDIR", "/tmp"), "bs_a.raw")
cap = subprocess.Popen(
    ["arecord", "-q", "-D", f"hw:{MIC},0", "-f", "S24_3LE", "-r", str(RATE),
     "-c", "2", "-d", str(SECS), "-t", "raw", raw],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(0.7)
play(Z, SECS - 0.5)
cap.wait(timeout=SECS + 8)
b = np.frombuffer(open(raw, "rb").read(), dtype=np.uint8)
b = b[:(b.size // 3) * 3].reshape(-1, 3)
v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
     (b[:, 2].astype(np.int32) << 16))
v = np.where(v & 0x800000, v - (1 << 24), v)
x = v[0::2].astype(np.float64)
spec = np.abs(np.fft.rfft(x - x.mean())) * 2.0 / x.size
k = int(round(TONE_HZ * x.size / RATE))
pk = float(np.max(spec[max(1, k - 3):k + 4]))
print(f"  [A zeros] EARS 1kHz L {20*math.log10(max(pk,1e-12)/2**23):+.1f} dBFS")
pcm_state("after-zeros")

print("\n-- 2. 'silent' check immediately after (ears does this as A0) --")
time.sleep(1.0)
after1 = ear_dbfs("silent1")
pcm_state("silent1")

print("\n-- 3. tone case, then silent again --")
play(T, SECS)
time.sleep(1.0)
after2 = ear_dbfs("silent2")
pcm_state("silent2")

print("\n== readout ==")
print(f"  fresh   L {base['L']:+.1f}  R {base['R']:+.1f}")
print(f"  silent1 L {after1['L']:+.1f}  R {after1['R']:+.1f}")
print(f"  silent2 L {after2['L']:+.1f}  R {after2['R']:+.1f}")
rise = max(after1["L"], after1["R"]) - max(base["L"], base["R"])
print(f"  floor rise fresh->silent1: {rise:+.1f} dB"
      f"   {'PERSISTENT STATE after first playback' if rise > 6 else 'no persistent floor'}")
