#!/usr/bin/env python3
"""First-stream-after-load pop vs reg 14 SOFT_START_TIME -- one firmware
load per value, because the pop is a COLD-START event.

Everything reconciles on that single fact: ears_matrix always measures the
FIRST stream after xrun load (case A is its first case), and the pop only
appears there -- case_a_timing saw the +7.6 dB first-second burst on the
first open and CLEAN deltas on every restart; the 150 ms mute-hold build
made case A WORSE (-29.5 vs -45.5) because it stretches the muted period
before that first unmute; softstart_probe PASSed every reg14 value because
it measures RESTARTS, where there is no pop to remove.

So: load board -> write reg14 over SCPI BEFORE any stream exists -> run
the first stream with the burst-window capture. Worst first-second delta
vs the no-stream floor decides. Usage: first_stream_pop.py <reg14-hex|keep>
"""
import base64
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
SECS = 4
# argv: zero or more 'RR=VV' hex pairs written over SCPI AFTER load and
# BEFORE any stream exists (the cold-start window). e.g. 1A=FF 0E=14.
# 'keep' (or no args) writes nothing and measures the stock boot state.
WRITES = []
for a in sys.argv[1:]:
    if a == "keep":
        continue
    m = re.fullmatch(r"([0-9a-fA-F]{2})=([0-9a-fA-F]{2})", a)
    if not m:
        sys.exit(f"bad arg {a!r} (want RR=VV hex or keep)")
    WRITES.append((int(m.group(1), 16), int(m.group(2), 16)))


def scpi(cmd, timeout=10):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def rd(addrs):
    tok = base64.b64encode(bytes(addrs)).decode()
    out = scpi(f'REGister:READ? "{tok}"').strip().strip('"')
    try:
        return list(base64.b64decode(out, validate=True))
    except Exception:
        return None


def wr(a, v):
    tok = base64.b64encode(bytes([a, v])).decode()
    return "ERROR" not in scpi(f'REGister:WRITE "{tok}"').upper()


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
    sys.exit("cards missing")
print(scpi("*IDN?"))
print("reg14 before:", rd([0x0E]))

# volume + mic BEFORE anything (these do not touch the codec stream path)
for line in subprocess.run(["amixer", "-c", str(MIC), "controls"],
                           capture_output=True, text=True).stdout.splitlines():
    if "Mic Capture Switch" in line and line.startswith("numid="):
        amixer(MIC, "cset", line.split(",", 1)[0].split("=")[1], "--", "on")
for nid in (5, 6):
    amixer(XMOS, "cset", f"numid={nid}", "0,0")
    time.sleep(0.1)
    amixer(XMOS, "cset", f"numid={nid}", "192,192")
scpi("GENerator:OFF")

for a, v in WRITES:
    if not wr(a, v) or (rd([a]) or [None])[0] != v:
        sys.exit(f"reg 0x{a:02X} := 0x{v:02X} failed")
    print(f"reg 0x{a:02X} := 0x{v:02X} (verified, BEFORE any stream)")

Z = os.path.join(os.environ.get("TMPDIR", "/tmp"), "fs_zeros.wav")
with wave.open(Z, "wb") as w:
    w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
    w.writeframes(b"\x00" * (3 * 2 * int(RATE * 12)))


def measure(tag, stream):
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    subprocess.run(["pkill", "-x", "ffmpeg"], capture_output=True)
    time.sleep(0.5)
    raw = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"fs_{tag}.raw")
    if os.path.exists(raw):
        os.remove(raw)
    cap = subprocess.Popen(
        ["arecord", "-q", "-D", f"hw:{MIC},0", "-f", "S24_3LE", "-r",
         str(RATE), "-c", "2", "-d", str(SECS), "-t", "raw", raw],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ff = None
    if stream:
        time.sleep(0.4)
        ff = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-t", str(SECS - 0.5), "-i", Z, "-f", "alsa", "-ac", "2",
             f"plughw:{XMOS},0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cap.wait(timeout=SECS + 10)
    if ff:
        ff.wait(timeout=10)
    b = np.frombuffer(open(raw, "rb").read(), dtype=np.uint8)
    b = b[:(b.size // 3) * 3].reshape(-1, 3)
    v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
         (b[:, 2].astype(np.int32) << 16))
    v = np.where(v & 0x800000, v - (1 << 24), v)
    out = {}
    for name, s in (("L", v[0::2]), ("R", v[1::2])):
        if int(np.max(np.abs(s))) < 32:
            print(f"  MIC DEAD ({name}) -- void"); sys.exit(3)
        per = []
        for i0 in range(0, s.size - RATE + 1, RATE):
            x = s[i0:i0 + RATE].astype(np.float64)
            spec = np.abs(np.fft.rfft(x - x.mean())) * 2.0 / x.size
            k = int(round(TONE_HZ * x.size / RATE))
            pk = float(np.max(spec[max(1, k - 3):k + 4]))
            per.append(20 * math.log10(max(pk, 1e-12) / 2 ** 23))
        out[name] = per
    return out


print("\nA0 (no stream ever opened on this board):")
a0 = measure("a0", stream=False)
for ch in ("L", "R"):
    print(f"  A0 {ch}: " + " ".join(f"{d:6.1f}" for d in a0[ch]))
base = max(max(a0[c]) for c in ("L", "R"))

print("\nFIRST stream on this board (the pop window):")
z = measure("first", stream=True)
worst = -999.0
for ch in ("L", "R"):
    d = [x - base for x in z[ch]]
    print(f"  Z {ch} deltas: " + " ".join(f"{x:+6.1f}" for x in d))
    worst = max(worst, max(d))
ok = worst < 6.0
print(f"\nFIRST-STREAM {sys.argv[1:] or ['stock']}: worst {worst:+.1f} dB"
      f"{'PASS' if ok else 'FAIL'} (gate < +6.0)")
sys.exit(0 if ok else 1)
