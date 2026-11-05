#!/usr/bin/env python3
"""Last tester variable: does the tap need a capture stream OPEN to be fed?

The tap posts samples toward the USB OUT (capture) endpoint. If nothing is
reading that endpoint, the TX call site may never run -- which would make the
all-zero counters my previous probe saw an artefact of the probe, not of the
firmware. So: hold a capture stream open, play into the DAC, and read the
device's own counters plus the captured bytes at the same time.
"""
import os, subprocess, sys, time
import numpy as np

REPO = os.path.expanduser("~/sw-audio-sc/sw_usb_audio/clearsound_uac")
CLIENT = os.path.join(REPO, "csd-js-tester", "csd_diag.py")
PY = sys.executable
DAC = 3


def sh(cmd, timeout=180):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def scpi(cmd, timeout=12):
    return sh([PY, CLIENT, "--backend", "rawmidi", "--timeout", str(timeout), cmd],
              timeout=timeout + 30)[1].strip().strip('"')


def show(tag):
    print("  %-26s LOOPback?=%-10s INDEX?=%s" % (tag, scpi("DEBUG:LOOPback?"),
                                                 scpi("DEBUG:LOOPback:INDEX?")))


print("IDN:", scpi("*IDN?"))
print()
print("--- state with NO streams open ---")
show("idle")

cap = "/tmp/tap_cap.wav"
if os.path.exists(cap):
    os.remove(cap)
print()
print("--- hold a CAPTURE stream open on hw:3,0 (S32_LE 48k 2ch), then play ---")
arec = subprocess.Popen(["arecord", "-q", "-D", "hw:%d,0" % DAC, "-f", "S32_LE",
                         "-r", "48000", "-c", "2", "-d", "20", "-t", "wav", cap],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.5)
show("capture open only")

scpi("DEBUG:LOOPback 1"); scpi("DEBUG:LOOPback:CLEAR")
play = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
                         "-f", "lavfi",
                         "-i", "sine=frequency=1000:sample_rate=48000:duration=10",
                         "-ac", "2", "-f", "alsa", "plughw:%d,0" % DAC],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.0)
for i in range(3):
    show("capture+playback t=%d" % i)
    time.sleep(1.2)
play.terminate()
try:
    play.wait(timeout=10)
except Exception:
    play.kill()
time.sleep(1.0)
arec.terminate()
try:
    arec.wait(timeout=15)
except Exception:
    arec.kill()

print()
print("--- what the capture stream actually received ---")
if os.path.exists(cap) and os.path.getsize(cap) > 1000:
    import wave
    with wave.open(cap, "rb") as w:
        n, chn = w.getnframes(), w.getnchannels()
        raw = np.frombuffer(w.readframes(n), dtype=np.int32).reshape(n, chn)
    for c in range(min(chn, 2)):
        nz = int((raw[:, c] != 0).sum())
        print("  ch%d: %d frames, %d nonzero (%.2f%%), peak %.1f dBFS, distinct %d" % (
            c, n, nz, 100.0 * nz / max(n, 1),
            20 * np.log10(np.abs(raw[:, c]).max() / 2147483648.0 + 1e-12),
            len(np.unique(raw[:, c]))))
    sp = np.abs(np.fft.rfft(raw[:, 0].astype(np.float64) * np.hanning(len(raw))))
    fr = np.fft.rfftfreq(len(raw), 1 / 48000.0)
    k = int(np.argmax(sp[10:])) + 10
    print("  strongest component in ch0: %.0f Hz" % fr[k])
    print("  (a 1 kHz peak means the capture mirrors playback: the tap IS feeding)")
else:
    print("  no capture file -- the capture stream would not open")
