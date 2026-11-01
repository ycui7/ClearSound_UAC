#!/usr/bin/env python3
"""Does the chip's soft-start ramp (reg 14 SOFT_START_TIME) kill the
stream-start unmute pop that ears case A measures?

Evidence ordering (case_a_timing.py + the 150 ms hold experiment):
  * steady zeros with boot automute: +0.8/+0.7 dB -> automute handles it
  * the residual A failure lives in the FIRST fraction of a second and
    SCALES with mute/unmute exposure: holding system mute 150 ms before
    releasing made case A WORSE (-29.5 vs -45.5 dBFS) -- a longer ramp-out
    at release means a bigger pop. That is an unmute transient.
Datasheet reg 14: SOFT_START_TIME [4:0], ramp = 4096 * 2^(TIME+1) / f_DATA;
boot writes reg 14 nowhere, so the chip unmutes on its default ramp.

Runtime test, no rebuild: write reg 14 with several TIME values, and after
each, force a fresh stream start (stop -> start), capturing the burst
window. Compare the first-second 1 kHz bin against the no-stream floor.
PASS = no second exceeds floor +6 dB. Registers snapshot/restore around it.
If a TIME value works, the boot fix is one DAC_REGWRITE next to the
automute block. If none does, the pop is not soft-start-gated and the
automute steady-state fix stands as the shipped improvement.
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
SNAP_ADDRS = [0x0E]


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


def wr_v(a, v):
    return wr(a, v) and (rd([a]) or [None])[0] == v


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

snap = rd(SNAP_ADDRS)
if snap is None:
    sys.exit("snapshot failed")
SNAP = dict(zip(SNAP_ADDRS, snap))
print("reg14 at boot:", {f"0x{a:02X}": f"0x{v:02X}" for a, v in SNAP.items()})

for line in subprocess.run(["amixer", "-c", str(MIC), "controls"],
                           capture_output=True, text=True).stdout.splitlines():
    if "Mic Capture Switch" in line and line.startswith("numid="):
        amixer(MIC, "cset", line.split(",", 1)[0].split("=")[1], "--", "on")
for nid in (5, 6):
    amixer(XMOS, "cset", f"numid={nid}", "0,0")
    time.sleep(0.1)
    amixer(XMOS, "cset", f"numid={nid}", "192,192")
scpi("GENerator:OFF")

Z = os.path.join(os.environ.get("TMPDIR", "/tmp"), "ss_zeros.wav")
with wave.open(Z, "wb") as w:
    w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
    w.writeframes(b"\x00" * (3 * 2 * int(RATE * 12)))


def measure(tag):
    """Fresh stream start, capture spans the unmute transient."""
    subprocess.run(["pkill", "-x", "ffmpeg"], capture_output=True)
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.6)
    raw = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"ss_{tag}.raw")
    if os.path.exists(raw):
        os.remove(raw)
    cap = subprocess.Popen(
        ["arecord", "-q", "-D", f"hw:{MIC},0", "-f", "S24_3LE", "-r",
         str(RATE), "-c", "2", "-d", str(SECS), "-t", "raw", raw],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.4)
    subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-t", str(SECS - 0.5), "-i", Z, "-f", "alsa", "-ac", "2",
         f"plughw:{XMOS},0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cap.wait(timeout=SECS + 8)
    subprocess.run(["pkill", "-x", "ffmpeg"], capture_output=True)
    time.sleep(0.4)
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


print("\n-- A0 floor (no stream) --")
a0 = measure("a0")
for ch in ("L", "R"):
    print(f"  A0 {ch}: " + " ".join(f"{d:6.1f}" for d in a0[ch]))
base = max(max(a0[c]) for c in ("L", "R"))

print("\n-- reg14 sweep, each with a fresh stream start --")
results = {}
for t in (0x00, 0x0A, 0x0F, 0x14):
    if not wr_v(0x0E, t):
        print(f"  TIME={t:#04x}: write failed, skipping"); continue
    d = measure(f"t{t:02X}")
    worst = -999.0
    line = []
    for ch in ("L", "R"):
        dl = [x - base for x in d[ch]]
        line.append(f"{ch} " + " ".join(f"{x:+6.1f}" for x in dl))
        worst = max(worst, max(dl))
    results[t] = worst
    print(f"  reg14={t:#04x}: worst delta {worst:+.1f} dB  "
          f"{'PASS' if worst < 6.0 else 'FAIL'}   | " + " | ".join(line))

print("\nrestore reg14:", end=" ")
print("OK" if wr_v(0x0E, SNAP[0x0E]) else "FAIL")

best = min(results, key=lambda t: results[t]) if results else None
if best is not None and results[best] < 6.0:
    print(f"\nRESULT: reg14 SOFT_START_TIME={best:#04x} clears the start pop"
          f" ({results[best]:+.1f} dB) -> add boot write of reg 14.")
else:
    print("\nRESULT: no soft-start value clears it; unmute pop is not"
          " ramp-gated by reg 14. Automute steady-state fix stands.")
