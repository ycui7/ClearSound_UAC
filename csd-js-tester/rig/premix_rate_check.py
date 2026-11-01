#!/usr/bin/env python3
"""emit/s must equal the stream frame rate.

The previous run proved the mixer thread emits (delta 318 929, sup_*=0) but
could not say at what rate: the elapsed time included SCPI retries after
empty replies, so frames/s was unknowable. It is not cosmetic -- every emit
advances the NCO exactly once, so emit/s IS the frame rate the oscillator
sees. If it were 3x the negotiated rate the tone would sit at 3 kHz while
every SCPI field still read correctly, and only the acoustic leg would ever
notice. Measured here directly, so the claim does not rest on the spectrum.

Reports emit/s against the rate the device itself reports in
GENerator:STATe? (field 1). Tolerance 5 %: the audio thread's ticks are
clock-derived, so drift over a 10 s window is far below that, and anything
larger is a real double-advance.
"""
import os
import re
import subprocess
import sys
import time

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
WINDOW = 10.0


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def nums(s):
    return [int(x) for x in re.findall(r"-?\d+", s)]


def card_number(pat):
    for line in open("/proc/asound/cards"):
        if pat in line.lower():
            m = re.search(r"^\s*(\d+)\s*\[", line)
            if m:
                return m.group(1)
    return None


XMOS = os.environ.get("CSD_XMOS_CARD") or card_number("xmos")
if not XMOS:
    sys.exit("no XMOS card")
print("playback card:", XMOS)

stream = subprocess.Popen(
    ["sg", "audio", "-c",
     f"ffmpeg -v error -f lavfi -i anullsrc=r=48000:cl=stereo -t 120 "
     f"-f alsa hw:{XMOS}"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(3.0)
if stream.poll() is not None:
    sys.exit(f"stream died rc={stream.poll()} -- mixer1 never runs")

scpi("GENerator:OFF")
time.sleep(0.2)
st = nums(scpi("GENerator:STATe?"))
print("state before arm:", st)

r = scpi("GENerator:SINe 48000,1000000,1073741824")
print("arm reply:", repr(r))
if r.upper().startswith("ERROR"):
    stream.terminate()
    sys.exit(f"arm rejected: {r!r}")

# Settle past any start-of-stream effects, then time a clean window. The
# bracketing queries measure the SCPI round trip so its latency can be
# subtracted from the window: 10 s of frames dwarfs it either way, but the
# number should be honest about what it includes.
time.sleep(1.0)
t0 = time.monotonic()
a = nums(scpi("DEBUG:GENerator:COUNts?"))
t1 = time.monotonic()
time.sleep(WINDOW)
t2 = time.monotonic()
b = nums(scpi("DEBUG:GENerator:COUNts?"))
t3 = time.monotonic()

scpi("GENerator:OFF")
time.sleep(0.2)
st2 = nums(scpi("GENerator:STATe?"))
stream.terminate()
try:
    stream.wait(timeout=5)
except subprocess.TimeoutExpired:
    stream.kill()

print("counters a:", a)
print("counters b:", b)
print("state after:", st2)

emit = b[0] - a[0]
# Window: from the mid-point of query a to the mid-point of query b.
mid_a = (t0 + t1) / 2.0
mid_b = (t2 + t3) / 2.0
dt = mid_b - mid_a
rate = emit / dt if dt > 0 else 0.0
fs_reported = st[1] if len(st) > 1 else 0
print(f"\nwindow: {dt:.3f} s (SCPI round trip ~{(t1-t0)*1000:.0f} ms)")
print(f"emit delta: {emit}   -> {rate:,.0f} frames/s")
print(f"device-reported fs: {fs_reported} Hz")
if fs_reported:
    ratio = rate / fs_reported
    print(f"ratio emit/s : fs = {ratio:.4f}")
    ok = abs(ratio - 1.0) < 0.05
    print("VERDICT:", "RATE CORRECT (one NCO step per frame)" if ok else
          f"RATE WRONG x{ratio:.3f} -- NCO double-advance or stream rate "
          "mismatch; tone frequency would be off by the same factor")
else:
    print("VERDICT: cannot judge, fs not reported")
