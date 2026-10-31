#!/usr/bin/env python3
"""Attribution probe: mixer build, stream EXACT zeros, listen at 1 kHz.

The ears gate case A failed on the mixer build (+10 dB at 1 kHz over the
no-stream baseline). Verdict so far, from runs of THIS rig plus the matrix:

  generator emitting while "OFF"  -> RULED OUT: emit delta == 0 across
                                     every zeros-stream window (device-side
                                     SCPI counters, independent of the mic)
  rig EMI / bench drift           -> LIKELY: the SAME production artifact
                                     passed case A at +5.7 dB one run and
                                     failed it at +6.2 dB 25 minutes later,
                                     and the "tone" sits 66 dB below case
                                     B's real tone with SNR +3.9

The counter half is trustworthy with the mic dead; the acoustic half needs
the mic alive, so a floor below -90 dBFS now voids the run (the EARS
capture switch reads 'on' while still streaming digital zeros -- trust
the floor, not the switch).

Order matters: force the EARS 'Mic Capture Switch' ON first (its only
control; a muted mic reads -122 dBFS and looks like a dead DAC -- this
probe measured that once and it meant nothing). Capture its OWN A0
no-stream baseline, then the zeros-stream window in the same session, so
drift between runs cannot fake the delta. Prints both plus generator
counters bracketing the same window.
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
SECS = 6


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def nums(s):
    return [int(x) for x in re.findall(r"-?\d+", s)]


def card_number(*pats):
    for line in open("/proc/asound/cards"):
        m = re.search(r"^\s*(\d+)\s*\[", line)
        if m and any(p in line.lower() for p in pats):
            return m.group(1)
    return None


XMOS = os.environ.get("CSD_XMOS_CARD") or card_number("xmos")
MIC = os.environ.get("CSD_MIC_CARD") or card_number("e36db", "e.a.r.s", "36db")
if not XMOS or not MIC:
    sys.exit(f"cards missing (XMOS={XMOS} MIC={MIC})")

# Force the EARS capture switch ON: it is the capsule's ONLY control, and a
# muted mic reads -122 dBFS and looks like a silent DAC (this probe did once).
sw = 0
out = subprocess.run(["amixer", "-c", MIC, "controls"],
                     capture_output=True, text=True).stdout
for line in out.splitlines():
    if "Mic Capture Switch" in line and line.startswith("numid="):
        nid = line.split(",", 1)[0].split("=")[1]
        subprocess.run(["amixer", "-q", "-c", MIC, "cset", f"numid={nid}",
                        "--", "on"], capture_output=True)
        sw += 1
if sw == 0:
    sys.exit("no Mic Capture Switch found -- a muted mic measures nothing")

# codec volume full, parked through 0 first (the kernel I2C cache dedups
# same-value writes, so a stale-volume case 'passes' without a SET_CUR)
for nid in (5, 6):
    subprocess.run(["amixer", "-q", "-c", XMOS, "cset", f"numid={nid}",
                    "0,0"], capture_output=True)
    time.sleep(0.2)
    subprocess.run(["amixer", "-q", "-c", XMOS, "cset", f"numid={nid}",
                    "192,192"], capture_output=True)
time.sleep(0.3)


def capture(tag):
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    raw = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"zeros_{tag}.raw")
    if os.path.exists(raw):
        os.remove(raw)
    subprocess.run(["arecord", "-q", "-D", f"hw:{MIC},0", "-f", "S24_3LE",
                    "-r", str(RATE), "-c", "2", "-d", str(SECS), "-t", "raw",
                    raw], capture_output=True)
    if not os.path.exists(raw) or os.path.getsize(raw) < 3 * 2048:
        sys.exit(f"no capture ({tag}) -- mic dead?")
    b = np.frombuffer(open(raw, "rb").read(), dtype=np.uint8)
    b = b[:(b.size // 3) * 3].reshape(-1, 3)
    v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
         (b[:, 2].astype(np.int32) << 16))
    v = np.where(v & 0x800000, v - (1 << 24), v)
    res = {}
    for name, s in (("L", v[0::2]), ("R", v[1::2])):
        x = s.astype(np.float64)
        spec = np.abs(np.fft.rfft(x - x.mean())) * 2.0 / s.size
        body = float(np.median(spec[1:]))
        k = int(round(1000.0 * s.size / RATE))
        pk1 = float(np.max(spec[max(1, k - 3):k + 4]))
        floor = 20 * math.log10(max(body, 1e-12) / 2 ** 23)
        tone = 20 * math.log10(max(pk1, 1e-12) / 2 ** 23)
        res[name] = (floor, tone)
        print(f"  [{tag:6s} mic{name}] floor {floor:7.1f}  1kHz {tone:7.1f}  "
              f"tonality {(pk1 / body if body > 0 else 0):8.1f}x")
    return res


print("boot:", scpi("GENerator:STATe?"), "| counts",
      scpi("GENerator:COUNts?"), "| gain", scpi("MIXer:GAIN?"))
scpi("GENerator:OFF")
c0 = nums(scpi("GENerator:COUNts?"))

print("A0 no-stream baseline:")
a0 = capture("a0")

Z = os.path.join(os.environ.get("TMPDIR", "/tmp"), "zeros_mix.wav")
with wave.open(Z, "wb") as w:
    w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
    w.writeframes(b"\x00" * (3 * 2 * int(RATE * (SECS + 4))))
play = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
     "-i", Z, "-f", "alsa", "-ac", "2", f"plughw:{XMOS},0"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2.0)
if play.poll() is not None:
    sys.exit("ffmpeg died before measuring anything")

print("zeros streaming:")
ca = capture("zeros")
c1 = nums(scpi("GENerator:COUNts?"))

# Decisive discrimination: keep streaming the SAME zeros and mute the codec.
# Case C proved volume 0 drops the ACOUSTIC path >=30 dB; if the zeros bump
# survives mute it is not the DAC making sound -- it is electrical pickup
# (the skill's content-invariance test), i.e. rig fault, not firmware.
for nid in (5, 6):
    subprocess.run(["amixer", "-q", "-c", XMOS, "cset", f"numid={nid}",
                    "0,0"], capture_output=True)
time.sleep(0.4)
print("zeros streaming, codec MUTED:")
cm = capture("mute")
play.terminate()
try:
    play.wait(timeout=10)
except subprocess.TimeoutExpired:
    play.kill()

for ch in ("L", "R"):
    d = ca[ch][1] - a0[ch][1]
    dm = cm[ch][1] - a0[ch][1]
    print(f"  mic{ch}: zeros-vs-A0 {d:+.1f} dB | zeros+mute-vs-A0 {dm:+.1f} dB"
          f"   {'PICKUP (bump survives mute)' if dm > 6.0 else 'acoustic-consistent'}")
# Mic-alive gate: EARS reads ~-120 dBFS digital zeros when its capture
# switch is off; a probe that measures silence proves nothing.
for ch in ("L", "R"):
    if max(a0[ch][0], ca[ch][0]) < -90.0:
        sys.exit(f"MIC DEAD (mic{ch} floor <-90 dBFS) -- capture switch off; "
                 "numbers above are void")
print("counts before:", c0, " after:", c1)
if len(c1) >= 1 and len(c0) >= 1:
    print("emit delta during zeros stream:", c1[0] - c0[0])
print("SCRIPT_RC=0")
