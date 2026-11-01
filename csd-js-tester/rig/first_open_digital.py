#!/usr/bin/env python3
"""Is the first-open pop DIGITAL? Capture the board's own post-DSP tap at
the very FIRST stream open after load -- no mic, no cable.

All codec-register theories (automute, system mute, soft start reg 14,
charge-pump delay reg 0x1A) are dropped: none moved the first-second
+9.5 dB click. The digital path was cleared only at STEADY STATE (mirror
and post-DSP taps both waited 2 s after ffmpeg started). This is the
missing measurement, and it is the only one that separates the two
remaining hypotheses:

  non-zero words at t=0 of the FIRST open -> OUR firmware (uninitialised
      or stale buffer emitted before host data arrives) -> fix in code.
  clean zeros at t=0 and mic still pops   -> codec analog startup, and the
      register knobs that control it are exhausted.

Tap: the app's CSD_DSP_DIAG_LOOP mirrors sampsFromUsbToAudio -- the exact
buffer handed to I2S -- onto the UAC2 capture payload, so with the capture
steering switches OFF, hw:<card>,0 streams the post-DSP, pre-I2S words.
dsp_tap_probe.py proved this tap carries a real tone (793 180x tonality),
so its silence is meaningful.

Geometry per load: arm arecord FIRST, then open the zeros stream, so the
capture starts before the first frame exists.
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
SECS = 4


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
if not XMOS:
    sys.exit("XMOS card missing")
print(scpi("*IDN?"))

# DIAG_LOOP tap = plain capture payload: steering switches must be OFF or
# the XU matrix steers the mixer-output mirror in instead (dsp_tap_probe).
out = subprocess.run(["amixer", "-c", str(XMOS), "controls"],
                     capture_output=True, text=True).stdout
for line in out.splitlines():
    if ("Mix 2 Capture Switch" in line or "Extension Unit Switch" in line) \
            and line.startswith("numid="):
        amixer(XMOS, "cset", line.split(",", 1)[0].split("=")[1], "--", "off")

Z = os.path.join(os.environ.get("TMPDIR", "/tmp"), "fsd_zeros.wav")
with wave.open(Z, "wb") as w:
    w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
    w.writeframes(b"\x00" * (3 * 2 * int(RATE * 12)))


def grab(tag):
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.4)
    raw = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"fsd_{tag}.raw")
    if os.path.exists(raw):
        os.remove(raw)
    cap = subprocess.Popen(
        ["arecord", "-q", "-D", f"hw:{XMOS},0", "-f", "S32_LE", "-r",
         str(RATE), "-c", "2", "-d", str(SECS), "-t", "raw", raw],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cap, raw


def windows(raw, label):
    v = np.frombuffer(open(raw, "rb").read(), dtype="<i4")
    if v.size < RATE:
        print(f"  {label}: capture too short ({v.size} words)")
        return None
    L = v[0::2]
    out = {}
    for name, ms in (("0-50ms", 50), ("50-150ms", 150), ("150-500ms", 500),
                     ("0.5-2s", 2000)):
        # windows are [.. , ms] measured from the END of each prior window
        pass
    # simpler: report running max |sample| over cumulative prefixes
    for cut_ms in (10, 50, 100, 250, 500, 1000, 2000):
        n = int(RATE * cut_ms / 1000)
        n = min(n, L.size)
        out[cut_ms] = int(np.max(np.abs(L[:n]))) if n else 0
    print(f"  {label}: max|s| of L by window end (2^31 = FS):  "
          + "  ".join(f"{c}ms:{v_}" for c, v_ in out.items()))
    return out


# VOLUME-RAMP HYPOTHESIS, tested first because it explains the outliers:
# a volume step on zero data still ramps whatever DC the idle modulator
# carries, which differentiates into a click in the AC-coupled analog path.
# Every rig parks volume 0 -> 192 before opening; the pop is present in all
# of them. 'novol' skips the step entirely (leave boot volume as-is).
NOVOL = "novol" in sys.argv
if NOVOL:
    print("novol: skipping the 0->192 park (volume step hypothesis)")
else:
    for nid in (5, 6):
        amixer(XMOS, "cset", f"numid={nid}", "0,0")
        time.sleep(0.1)
        amixer(XMOS, "cset", f"numid={nid}", "192,192")

print("\n-- A0: capture open, NO stream ever --")
cap, raw = grab("a0")
cap.wait(timeout=SECS + 8)
a0 = windows(raw, "A0")

print("\n-- FIRST open: capture armed first, zeros stream joins 0.3 s in --")
cap, raw = grab("first")
time.sleep(0.3)
ff = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-t", str(SECS - 0.2),
     "-i", Z, "-f", "alsa", "-ac", "2", f"plughw:{XMOS},0"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
cap.wait(timeout=SECS + 10)
ff.wait(timeout=10)
z = windows(raw, "FIRST-open")

print("\n-- SECOND open (control: the pop only ever appears on the first) --")
cap, raw = grab("second")
time.sleep(0.3)
ff = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-t", str(SECS - 0.2),
     "-i", Z, "-f", "alsa", "-ac", "2", f"plughw:{XMOS},0"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
cap.wait(timeout=SECS + 10)
ff.wait(timeout=10)
z2 = windows(raw, "SECOND-open")

print("\n== readout ==")
if z is None:
    sys.exit("first-open capture failed")
peak_first = min(z.values()) if z else None
biggest = max(z.values())
print(f"  first-open biggest |sample| = {biggest} of 2^31 "
      f"({20*math.log10(max(biggest,1)/2**31):.1f} dBFS)")
if biggest > (1 << 20):
    print("  DIGITAL BURST at first open: firmware emits non-zero words"
          " before/around host data -> find and fix the module.")
elif a0 is not None and max(a0.values()) < (1 << 12) and biggest < (1 << 12):
    print("  digital path CLEAN at first open (<=2^-19 FS): the pop is not"
          " made by any firmware-owned buffer -- no digital fix exists.")
else:
    print(f"  inconclusive: A0 max {max(a0.values()) if a0 else '?'}, first"
          f" {biggest}; MAC/USB jitter floor expected ~100 LSB")
