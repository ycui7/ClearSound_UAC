#!/usr/bin/env python3
"""Where in time does the case-A artifact live?

ears_matrix starts arecord BEFORE playback, so its 3 s window covers stream
start. The A/B rig starts ffmpeg first and captures ~4 s later, and its
identical-zeros control PASSED the case-A metric. Same build, same bench,
opposite verdicts: the difference is capture timing, so time-resolve the
artifact instead of arguing about it.

Per zeros-stream: arecord a 7 s window, and 0.2 s into it start the ffmpeg
zeros stream -- the window therefore covers t=0 stream start, the DPLL/ASRC
re-lock, and steady state. Split into 1 s frames; report the 1 kHz bin per
frame. A burst confined to the first ~2 s is a STREAM-START transient (the
codec re-locking its DPLL on returning DATA_CLK with the analog stage
unmuted -- a firmware-gateable event: mute across stream start), and
explains every observation at once:

  * it exists only while a stream is ACTIVE        (A0 clean, A dirty)
  * it is content-, volume-, channel-invariant     (idle transient)
  * it is exactly ~1 kHz                            (chip/DPLL scale, and
                               the rig notes already record a 1 kHz
                               component in this bench)
  * rigs with different window placement get      (timing-dependent)
    opposite verdicts

Steady-state frames must be at the A0 level for that diagnosis; a burst
that instead runs the whole window means the artifact is persistent and
this rig just changes the question to 'what generates it continuously'.
"""
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
SECS = 7


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


def amixer(card, *args):
    return subprocess.run(["amixer", "-q", "-c", str(card)] + list(args),
                          capture_output=True, text=True)


XMOS = os.environ.get("CSD_XMOS_CARD") or card_number("xmos")
MIC = os.environ.get("CSD_MIC_CARD") or card_number("e36db", "e.a.r.s", "36db")
if not XMOS or not MIC:
    sys.exit(f"cards missing (XMOS={XMOS} MIC={MIC})")
print(scpi("*IDN?"))

for line in subprocess.run(["amixer", "-c", str(MIC), "controls"],
                           capture_output=True, text=True).stdout.splitlines():
    if "Mic Capture Switch" in line and line.startswith("numid="):
        amixer(MIC, "cset", line.split(",", 1)[0].split("=")[1], "--", "on")
for nid in (5, 6):
    amixer(XMOS, "cset", f"numid={nid}", "0,0")
    time.sleep(0.1)
    amixer(XMOS, "cset", f"numid={nid}", "192,192")
scpi("GENerator:OFF")

Z = os.path.join(os.environ.get("TMPDIR", "/tmp"), "tim_zeros.wav")
n = int(RATE * 30)
buf = bytearray(b"\x00" * 3 * 2 * n)
with wave.open(Z, "wb") as w:
    w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
    w.writeframes(bytes(buf))


def play(wav):
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
         "-i", wav, "-f", "alsa", "-ac", "2", f"plughw:{XMOS},0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(p):
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
    time.sleep(0.4)


def rec(tag, secs=SECS):
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    raw = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"tim_{tag}.raw")
    if os.path.exists(raw):
        os.remove(raw)
    cap = subprocess.Popen(
        ["arecord", "-q", "-D", f"hw:{MIC},0", "-f", "S24_3LE", "-r",
         str(RATE), "-c", "2", "-d", str(secs), "-t", "raw", raw],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cap, raw


def per_second_db(raw):
    b = np.frombuffer(open(raw, "rb").read(), dtype=np.uint8)
    b = b[:(b.size // 3) * 3].reshape(-1, 3)
    v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
         (b[:, 2].astype(np.int32) << 16))
    v = np.where(v & 0x800000, v - (1 << 24), v)
    out = {}
    for name, s in (("L", v[0::2]), ("R", v[1::2])):
        if int(np.max(np.abs(s))) < 32:
            print(f"  MIC DEAD on {name} -- void run"); sys.exit(3)
        frames = []
        for i0 in range(0, s.size - RATE, RATE):
            x = s[i0:i0 + RATE].astype(np.float64)
            spec = np.abs(np.fft.rfft(x - x.mean())) * 2.0 / x.size
            k = int(round(TONE_HZ * x.size / RATE))
            pk = float(np.max(spec[max(1, k - 3):k + 4]))
            frames.append(20 * math.log10(max(pk, 1e-12) / 2 ** 23))
        out[name] = frames
    return out


print("\n-- A0 baseline (no stream), same window length --")
cap, raw = rec("a0"); cap.wait(timeout=SECS + 8)
a0 = per_second_db(raw)
for ch in ("L", "R"):
    print(f"  A0 {ch} per-second 1kHz: "
          + " ".join(f"{d:6.1f}" for d in a0[ch]))

print("\n-- zeros stream, capture starts 0.2 s BEFORE ffmpeg --")
cap, raw = rec("s")
time.sleep(0.2)
p = play(Z)
cap.wait(timeout=SECS + 8)
stop(p)
z = per_second_db(raw)
for ch in ("L", "R"):
    print(f"  Z  {ch} per-second 1kHz: "
          + " ".join(f"{d:6.1f}" for d in z[ch]))
    base = max(a0[ch])
    d = [x - base for x in z[ch]]
    early = [x for x in d[:3]]
    late = d[4:]
    print(f"     delta vs A0:      " + " ".join(f"{x:+6.1f}" for x in d))
    burst = any(x >= 6.0 for x in early)
    steady_bad = any(x >= 6.0 for x in late)
    print(f"     -> early-window >=+6 dB: {burst}   late-window >=+6 dB: "
          f"{steady_bad}")
    if burst and not steady_bad:
        print("     DIAGNOSIS: stream-start transient only -> firmware must"
              " gate the codec (system mute) across stream start/stop")
    elif not burst and not steady_bad:
        print("     DIAGNOSIS: no artifact in this placement (window or"
              " bench state) -- rerun; ears window covers t=0, compare")
    elif steady_bad:
        print("     DIAGNOSIS: persistent artifact even at steady state")

print("\n-- stop and restart the stream: is it EVERY start or first only? --")
for k in range(2):
    cap, raw = rec(f"r{k}")
    time.sleep(0.2)
    p = play(Z)
    cap.wait(timeout=SECS + 8)
    stop(p)
    z = per_second_db(raw)
    for ch in ("L", "R"):
        d = [x - max(a0[ch]) for x in z[ch]]
        print(f"  restart{k} {ch} deltas: " + " ".join(f"{x:+6.1f}" for x in d))
