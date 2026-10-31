#!/usr/bin/env python3
"""Automute verification: did boot actually program regs 2/4/5, and does the
chip's automute respond to content?

Fix under test: boot now writes reg2=0x50 (2'b01 MUTE-on-automute,
ANALOG_AUTOMUTE=0 so reg 3 is left as programmed, TIMER_FAST=1, mix map at
reset), reg4 (AUTOMUTE_TIME)=128 -> ~5 ms at 48 kHz, reg5 (LEVEL)=104 ->
-104 dB. Reg 4's reset value 0 means 'automute disabled' per datasheet v1.3;
boot never wrote these, so digital silence left the analog stage live and
ears case-A heard chip idle artifacts as a fake 1 kHz.

Proof, all over SCPI (REGister:READ? -> tile0 I2C):
  1. regs 2/4/5 read back exactly the programmed bytes  (write landed)
  2. during a ZEROS stream, after settle, reg 0x48 shows AUTOMUTE_L/R=1
     (chip sees silence and mutes)                    (feature is alive)
  3. during a 1 kHz tone, reg 0x48 shows AUTOMUTE_L/R=0 (real data holds it
     released -- a fix that mutes forever is worse than no fix)

Status bits (mirror the boot report's decode): reg 0x48 bit7 OC_R, bit6
OC_L, bit5 AUTOMUTE_R, bit4 AUTOMUTE_L.
"""
import math
import os
import re
import subprocess
import sys
import time
import wave

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
RATE = 48000
fails = []


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


import base64

def reg(addr):
    """REGister:READ? takes a base64 list of addresses and answers base64
    of the byte values (same codec, same tile0 I2C path, same encoding as
    the identity reads)."""
    tok = base64.b64encode(bytes([addr])).decode()
    out = scpi(f"REGister:READ? {tok}")
    out = out.strip().strip('"')
    try:
        vals = base64.b64decode(out, validate=True)
    except Exception:
        return None
    return vals[0] if vals else None


def check(cond, what, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {what}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        fails.append(what)


print("== 1. boot programmed the automute registers ==")
r2, r4, r5 = reg(0x02), reg(0x04), reg(0x05)
check(r2 == 0x50, "reg2 MIXING_N_AUTOMUTE == 0x50", f"got {r2}")
check(r4 == 128, "reg4 AUTOMUTE_TIME == 128 (~5 ms)", f"got {r4}")
check(r5 == 104, "reg5 AUTOMUTE_LEVEL == 104 (-104 dB)", f"got {r5}")
if None in (r2, r4, r5):
    sys.exit("register readback unavailable -- fix is unverified")


def stim(path, tone):
    n = int(RATE * 8)
    buf = bytearray()
    for i in range(n):
        v = 0
        if tone:
            v = int(round(0.5 * (2 ** 23 - 1)
                          * math.sin(2 * math.pi * 1000.0 * i / RATE)))
        u = v & 0xFFFFFF
        word = bytes((u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF))
        buf += word + word
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
        w.writeframes(bytes(buf))


def card_number(*pats):
    for line in open("/proc/asound/cards"):
        m = re.search(r"^\s*(\d+)\s*\[", line)
        if m and any(p in line.lower() for p in pats):
            return m.group(1)
    return None


XMOS = os.environ.get("CSD_XMOS_CARD") or card_number("xmos")
if not XMOS:
    sys.exit("XMOS card missing")

scpi("GENerator:OFF")
T = os.path.join(os.environ.get("TMPDIR", "/tmp"), "am_tone.wav")
Z = os.path.join(os.environ.get("TMPDIR", "/tmp"), "am_zeros.wav")
stim(T, True); stim(Z, False)

# volumes full so the digital volume cannot itself look like silence
for nid in (5, 6):
    subprocess.run(["amixer", "-q", "-c", XMOS, "cset", f"numid={nid}", "0,0"],
                   capture_output=True)
    time.sleep(0.15)
    subprocess.run(["amixer", "-q", "-c", XMOS, "cset", f"numid={nid}",
                    "192,192"], capture_output=True)


def play(wav):
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
         "-i", wav, "-f", "alsa", "-ac", "2", f"plughw:{XMOS},0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def automute_bits():
    """Poll reg 0x48; return (asserted_seen_L, asserted_seen_R, last)."""
    sl = sr = False
    last = None
    for _ in range(30):
        v = reg(0x48)
        if v is None:
            return None, None, None
        last = v
        sl = sl or bool(v & 0x10)
        sr = sr or bool(v & 0x20)
        time.sleep(0.1)
    return sl, sr, last


print("\n== 2. zeros stream -> automute ASSERTS ==")
p = play(Z); time.sleep(2.0)
if p.poll() is not None:
    sys.exit("ffmpeg died (zeros)")
time.sleep(1.0)                       # > reg4 window (5 ms) x huge margin
sl, sr, last = automute_bits()
p.terminate()
try:
    p.wait(timeout=10)
except subprocess.TimeoutExpired:
    p.kill()
check(bool(sl) and bool(sr), "AUTOMUTE_L/R set during zeros",
      f"0x48 last={last}")

print("\n== 3. 1 kHz tone -> automute RELEASED ==")
p = play(T); time.sleep(2.0)
if p.poll() is not None:
    sys.exit("ffmpeg died (tone)")
time.sleep(0.5)
sl2, sr2, last2 = automute_bits()
p.terminate()
try:
    p.wait(timeout=10)
except subprocess.TimeoutExpired:
    p.kill()
check(not sl2 and not sr2, "AUTOMUTE stays clear during tone",
      f"0x48 last={last2}")

print(f"\nAUTOMUTE: {'PASS' if not fails else 'FAIL'} ({len(fails)})")
sys.exit(1 if fails else 0)
