#!/usr/bin/env python3
"""INDIVIDUAL volume control: host stream vs generator, IN THE MIXER.

User requirement: control each source's volume in the mixer graph, NOT as a
whole via the DAC/codec register. Implementation: two Q15 weights applied
at the pre-mixer injection point (csd_gen_mix_sample), set over SCPI as
MIXer:GAIN:HOST / MIXer:GAIN:GENerator, independent of each other and of
the single UAC 'Mix 2 Playback Volume' output multiplier.

What makes this test mean 'individual' and not 'some gain moved':

  baseline  host 3 kHz + tone 1 kHz, both weights unity -> both audible
  host leg  sweep w_host 32768->8192 (-12 dB): HOST must drop >=10 dB and
            the TONE must stay within +-3 dB  <-- the tone staying put is
            the whole point; a shared gain would move both
  gen  leg  sweep w_gen  32768->8192 (-12 dB): TONE must drop >=10 dB and
            the HOST must stay within +-3 dB
  restore   back to unity: both return within +-3 dB of baseline

Falsification control baked in: the UAC output multiplier is NEVER touched
here (boot gate asserts it is unity and untouched, same lesson as
premix_tone_check). If this rig passed by secretly moving the output
volume, both legs would drop together and the +-3 dB halves would fire.

Levels judged by tonality + relative dB only (bench gain, mic floor, and
the un-normalized-FFT trap are all documented in host_stream_check).
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
HOST_HZ, GEN_HZ = 3000.0, 1000.0
AMP_M6DBFS = 0x40000000
HOST_AMP = 0.5
UNITY = 32768
QTR = 8192                      # -12 dB
STIM = os.path.join(os.environ.get("TMPDIR", "/tmp"), "gain_host_3k.wav")
TONAL_MIN = 1000.0
fails = []


def check(cond, what, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {what}"
          + (f"  [{extra}]" if extra else ""))
    if not cond:
        fails.append(what)


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


def write_stim():
    n = int(RATE * 4.0)
    buf = bytearray()
    for i in range(n):
        v = int(round(HOST_AMP * (2 ** 23 - 1)
                      * math.sin(2 * math.pi * HOST_HZ * i / RATE)))
        u = v & 0xFFFFFF
        word = bytes((u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF))
        buf += word + word
    with wave.open(STIM, "wb") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
        w.writeframes(bytes(buf))
    # self-check: the 2x-rate interleave bug already cost three board cycles
    with wave.open(STIM) as w:
        raw = w.readframes(w.getnframes())
    b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
    v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
         (b[:, 2].astype(np.int32) << 16))
    v = np.where(v & 0x800000, v - (1 << 24), v).astype(np.float64)[0::2]
    sp = np.abs(np.fft.rfft(v - v.mean()))
    f = (int(np.argmax(sp[1:])) + 1) * RATE / v.size
    if abs(f - HOST_HZ) > 50:
        sys.exit(f"STIMULUS WRONG: {f:.1f} Hz != {HOST_HZ}")
    print(f"  stimulus self-check: {f:.1f} Hz ok")


def band(x, f, n):
    spec = np.abs(np.fft.rfft(x - x.mean())) * 2.0 / float(n)
    k = int(round(f * n / RATE))
    lo, hi = max(1, k - 3), min(len(spec) - 1, k + 3)
    pk = float(np.max(spec[lo:hi + 1]))
    body = float(np.median(spec[1:]))
    return (-300.0 if pk <= 0 else 20 * math.log10(pk / float(1 << 23)),
            pk / body if body > 0 else float("inf"))


def capture(tag):
    raw = os.path.join(os.environ.get("TMPDIR", "/tmp"), "gain_mix.raw")
    if os.path.exists(raw):
        os.remove(raw)
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    r = subprocess.run(["arecord", "-q", "-D", f"hw:{MIC},0", "-f",
                        "S24_3LE", "-r", str(RATE), "-c", "2", "-d", "1",
                        "-t", "raw", raw], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(raw):
        return None
    b = np.frombuffer(open(raw, "rb").read(), dtype=np.uint8)
    if b.size < 3 * 2048:
        return None
    b = b[:(b.size // 3) * 3].reshape(-1, 3)
    v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
         (b[:, 2].astype(np.int32) << 16))
    v = np.where(v & 0x800000, v - (1 << 24), v)
    n = v[0::2].size
    out = {}
    for name, s in (("L", v[0::2]), ("R", v[1::2])):
        out[name] = (band(s, HOST_HZ, n), band(s, GEN_HZ, n))
        print(f"        [{tag} {name}] host {out[name][0][0]:7.2f} dB/"
              f"{out[name][0][1]:7.0f}x   gen {out[name][1][0]:7.2f} dB/"
              f"{out[name][1][1]:7.0f}x")
    return out


def mean_level(caps, src, chs=("L", "R")):
    """src 0 = host, 1 = gen; average dB over channels."""
    return sum(caps[ch][src][0] for ch in chs) / len(chs)


print("== 0. identity + boot gates (weights AND UAC output multiplier) ==")
print("  ", scpi("*IDN?"))
g = nums(scpi("MIXer:GAIN?"))
check(len(g) == 2 and g[0] == UNITY and g[1] == UNITY,
      "both source weights start at Q15 unity", str(g))
v = (nums(scpi("GENerator:VOLume?")) + [0, 0, 0])[:3]
udb = float("-inf") if v[2] <= 0 else 20 * math.log10(v[2] / float(1 << 29))
check(abs(udb) < 0.5, "UAC output multiplier unity, untouched",
      f"{udb:.2f} dB")
if len(g) != 2 or abs(udb) >= 0.5:
    sys.exit("boot state contaminated -- refusing to attribute gains")

scpi("GENerator:OFF")
if os.path.exists(STIM):
    os.remove(STIM)
write_stim()
play = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
     "-i", STIM, "-f", "alsa", "-ac", "2", f"plughw:{XMOS},0"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2.0)
if play.poll() is not None:
    sys.exit("ffmpeg died before measuring anything")

try:
    r = scpi(f"GENerator:SINe {RATE},{int(GEN_HZ*1000)},{AMP_M6DBFS}")
    if r.upper().startswith("ERROR"):
        sys.exit(f"arm rejected: {r!r}")
    time.sleep(0.5)

    print("\n== 1. baseline: both unity, both audible ==")
    base = capture("unity")
    if base is None:
        sys.exit("no capture")
    for ch in ("L", "R"):
        check(base[ch][0][1] > TONAL_MIN, f"{ch} host audible", "")
        check(base[ch][1][1] > TONAL_MIN, f"{ch} tone audible", "")
    base_h, base_g = mean_level(base, 0), mean_level(base, 1)

    print("\n== 2. host leg: w_host 32768->8192 drops HOST, not tone ==")
    scpi(f"MIXer:GAIN:HOST {QTR}")
    back = nums(scpi("MIXer:GAIN?"))
    check(back == [QTR, UNITY], "weights read back as set", str(back))
    time.sleep(0.4)
    c = capture("host-qtr")
    if c:
        dh, dg = mean_level(c, 0) - base_h, mean_level(c, 1) - base_g
        check(dh <= -10.0, "host dropped >=10 dB with w_host=1/4",
              f"{dh:+.1f} dB (want -12)")
        check(abs(dg) <= 3.0, "tone UNCHANGED (individuality)", f"{dg:+.1f} dB")
    scpi(f"MIXer:GAIN:HOST {UNITY}")

    print("\n== 3. generator leg: w_gen 32768->8192 drops TONE, not host ==")
    scpi(f"MIXer:GAIN:GENerator {QTR}")
    back = nums(scpi("MIXer:GAIN?"))
    check(back == [UNITY, QTR], "weights read back as set", str(back))
    time.sleep(0.4)
    c = capture("gen-qtr")
    if c:
        dg = mean_level(c, 1) - base_g
        dh = mean_level(c, 0) - base_h
        check(dg <= -10.0, "tone dropped >=10 dB with w_gen=1/4",
              f"{dg:+.1f} dB (want -12)")
        check(abs(dh) <= 3.0, "host UNCHANGED (individuality)", f"{dh:+.1f} dB")

    print("\n== 4. both muted, then restored ==")
    scpi("MIXer:GAIN:HOST 0")
    scpi("MIXer:GAIN:GENerator 0")
    time.sleep(0.4)
    c = capture("muted")
    if c:
        for ch in ("L", "R"):
            check(c[ch][0][1] < TONAL_MIN and c[ch][1][1] < TONAL_MIN,
                  f"{ch} both sources muted", "")
    scpi(f"MIXer:GAIN:HOST {UNITY}")
    scpi(f"MIXer:GAIN:GENerator {UNITY}")
    time.sleep(0.4)
    c = capture("restored")
    if c:
        dh = mean_level(c, 0) - base_h
        dg = mean_level(c, 1) - base_g
        check(abs(dh) <= 3.0, "host restored", f"{dh:+.1f} dB")
        check(abs(dg) <= 3.0, "tone restored", f"{dg:+.1f} dB")
finally:
    scpi("GENerator:OFF")
    scpi(f"MIXer:GAIN:HOST {UNITY}")
    scpi(f"MIXer:GAIN:GENerator {UNITY}")
    play.terminate()
    try:
        play.wait(timeout=10)
    except subprocess.TimeoutExpired:
        play.kill()

print(f"\nMIXER GAIN: {'PASS' if not fails else 'FAIL'} ({len(fails)} failures)")
for w in fails:
    print("   -", w)
sys.exit(1 if fails else 0)
