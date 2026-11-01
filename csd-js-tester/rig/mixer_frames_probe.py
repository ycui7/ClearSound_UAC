#!/usr/bin/env python3
"""Does mixer1's sample loop actually run during a plain host stream?

Every case-A theory since the ladder has been about mixer-side work
(injection, gains, doMix, output volume, uninitialised mixer arrays). The
ladder (factory +1.0 dB, production +5.8, mixer-no-gen +4..7, mixer+gen
+11.7) implicates the mixer build, but no measurement so far has PROVEN the
mixer thread runs while a normal PCM stream plays. If MIXer:FRAMES? sits
still, the mixer is not in the stream path at all on this configuration and
every one of those theories is void -- which would be the most valuable
result of the day.
"""
import os, subprocess, sys, time, wave

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
RATE, SECS = 48000, 3


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def frames():
    s = scpi("MIXer:FRAMES?")
    try:
        return int(s)
    except ValueError:
        return None


def card(*pats):
    for line in open("/proc/asound/cards"):
        for p in pats:
            if p in line.lower():
                return line.split("[", 1)[0].strip() or line.split()[0]
    return None


XMOS = card("xmos", "clearsound")
if not XMOS:
    sys.exit("XMOS card not found")
print(scpi("*IDN?"))

f0 = frames()
if f0 is None:
    sys.exit("MIXer:FRAMES? unsupported -- rebuild with the counter")
print(f"frames before stream: {f0}")

Z = os.path.join(os.environ.get("TMPDIR", "/tmp"), "mf_zeros.wav")
with wave.open(Z, "wb") as w:
    w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
    w.writeframes(b"\x00" * (3 * 2 * int(RATE * (SECS + 1))))
subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", Z,
                "-f", "alsa", "-ac", "2", f"plughw:{XMOS},0"],
               capture_output=True, text=True, timeout=60)

f1 = frames()
print(f"frames after  {SECS}s zeros stream: {f1}")
d = (f1 - f0) if (f0 is not None and f1 is not None) else None
print(f"delta: {d} frames  ({(d or 0) / SECS:.0f} frames/s; expect ~{RATE})")

if d is None:
    print("VERDICT: INCONCLUSIVE (counter read failed)")
    sys.exit(2)
if d < RATE // 4:
    print(f"VERDICT: mixer1 DID NOT run during the stream (delta {d}).")
    print("  The mixer is not in this stream's path. All mixer-path case-A")
    print("  theories (injection, gains, doMix, output volume, init arrays)")
    print("  are VOID. Look elsewhere: what the mixer build changes about")
    print("  stream START/STOP signalling, not about sample processing.")
    sys.exit(1)
print("VERDICT: mixer1 runs with the host stream (loop is live).")
sys.exit(0)
