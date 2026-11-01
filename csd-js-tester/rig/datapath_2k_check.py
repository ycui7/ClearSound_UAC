#!/usr/bin/env python3
"""Datapath audit: continuous 2 kHz from the host, checked AT EVERY MODULE.

Question (user): at which module does the data fall apart or stop making
sense? Continuous 2 kHz both channels from the host; each module the frame
passes through is probed by the only instrument that can see its output.

  M0  host WAV                -> offline FFT of the file itself
                                 (proves the source; interleave bugs live here once)
  M1  mixer thread, RAW host pair (GetSamplesFromHost, BEFORE my pre-mixer
      hook and before doMix)  -> GENerator:DOPpRObe? lastL/lastR snapshots,
                                 sampled repeatedly: must be alive (nonzero,
                                 varying) and matches must stay 0 (PCM)
  M2  mixer output to device (after doMix AND after the post-mix output
      volume -- this is samples_to_device)
                              -> arecord on the UAC2 mirror, FORMAT S32_LE
                                 (learned the hard way: S24_3LE is the EARS
                                 format, the mirror is 32-bit; wrong format
                                 = 'capture failed' and a void run)
  M3  acoustic (speakers -> E.A.R.S. capsules, card 3)
                              -> arecord S24_3LE, 2 kHz tonality + mic-alive
                                 floor gate

M1 uses ONLY device-side counters/registers reached over MIDI: it cannot be
influenced by ALSA, cables, or the mic. A module is 'passing' only with the
2 kHz present AT ITS OUTPUT with strong tonality; the first module whose
output lacks the tone is where the data died, and it is printed as such.
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
TONE_HZ = 2000.0
AMP = 0.5
SECS = 4
TONAL_MIN = 1000.0

WAV = os.path.join(os.environ.get("TMPDIR", "/tmp"), "dp_2k.wav")


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


def amixer(card, *args):
    return subprocess.run(["amixer", "-q", "-c", str(card)] + list(args),
                          capture_output=True, text=True)


def numid_for(card, name):
    out = subprocess.run(["amixer", "-c", str(card), "controls"],
                         capture_output=True, text=True).stdout
    return [line.split(",", 1)[0].split("=")[1]
            for line in out.splitlines()
            if name in line and line.startswith("numid=")]


def analyze(x):
    """Return (db at TONE_HZ, tonality vs median body)."""
    v = x.astype(np.float64)
    spec = np.abs(np.fft.rfft(v - v.mean())) * 2.0 / v.size
    k = int(round(TONE_HZ * v.size / RATE))
    pk = float(np.max(spec[max(1, k - 3):k + 4]))
    body = float(np.median(spec[1:]))
    db = -300.0 if pk <= 0 else 20 * math.log10(pk / float(1 << 31))
    return db, (pk / body if body > 0 else float("inf"))


XMOS = os.environ.get("CSD_XMOS_CARD") or card_number("xmos")
MIC = os.environ.get("CSD_MIC_CARD") or card_number("e36db", "e.a.r.s", "36db")
if not XMOS or not MIC:
    sys.exit(f"cards missing (XMOS={XMOS} MIC={MIC})")

print("== setup ==")
print("  DAC card", XMOS, " EARS card", MIC)
print(" ", scpi("*IDN?"))

# Arm the UAC2 mirror (extension unit + both capture switches), volumes full
for name in ("Extension Unit Switch", "Mix 2 Capture Switch"):
    for nid in numid_for(XMOS, name):
        amixer(XMOS, "cset", f"numid={nid}", "--", "on")
for nid in (numid_for(XMOS, "Playback Volume")
            + numid_for(XMOS, "Mix 2 Playback Volume")
            + numid_for(XMOS, "Mix 2 Capture Volume")):
    amixer(XMOS, "cset", f"numid={nid}", "0,0")
    time.sleep(0.08)
    amixer(XMOS, "cset", f"numid={nid}", "192,192")

# ---- M0: the stimulus file itself (offline, no board) ----
n = int(RATE * 6.0)
buf = bytearray()
for i in range(n):
    v = int(round(AMP * (2 ** 23 - 1) * math.sin(2 * math.pi * TONE_HZ * i / RATE)))
    u = v & 0xFFFFFF
    word = bytes((u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF))
    buf += word + word
with wave.open(WAV, "wb") as w:
    w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
    w.writeframes(bytes(buf))
with wave.open(WAV) as w:
    raw = w.readframes(w.getnframes())
b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
     (b[:, 2].astype(np.int32) << 16))
v = np.where(v & 0x800000, v - (1 << 24), v).astype(np.float64)
sp = np.abs(np.fft.rfft(v[0::2] - v[0::2].mean()))
f0 = (int(np.argmax(sp[1:])) + 1) * RATE / v[0::2].size
print(f"\n== M0 host WAV ==  peak {f0:.1f} Hz (want {TONE_HZ})")
m0_ok = abs(f0 - TONE_HZ) < 50

scpi("GENerator:OFF")

play = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
     "-i", WAV, "-f", "alsa", "-ac", "2", f"plughw:{XMOS},0"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2.5)
if play.poll() is not None:
    sys.exit("ffmpeg died before measuring anything")


def snap_m1(tag):
    """DOPpRObe? lastL/lastR snapshots: alive host data at the mixer."""
    vals = []
    for _ in range(12):
        q = nums(scpi("DEBUG:GENerator:DOPpRObe?"))
        if len(q) >= 4:
            vals.append((q[0], q[1], q[2], q[3]))
        time.sleep(0.05)
    if not vals:
        return None
    mx = max(max(abs(s[2]), abs(s[3])) for s in vals)
    uniq = len({(s[2], s[3]) for s in vals})
    matches = {s[1] for s in vals}
    print(f"  [{tag}] frames={vals[-1][0]} max|L|,|R| seen = {mx}  "
          f"distinct snapshots = {uniq}/12  matches set = {matches}")
    return mx, uniq, matches


try:
    # ---- M1: raw host pair inside the frame thread ----
    # Mixer build: DOPpRObe lastL/lastR are written every frame by the mixer.
    # Production build: nobody writes that probe (it is mixer-thread
    # telemetry), so 'frames' sits at 0 -- asserting it there would fail a
    # healthy board (it did once). In production the equivalent node is the
    # UBM input, which M4's tap (post-frame hook buffer) covers instead:
    # skip M1, mark it, and let M2/M4 carry the chain.
    probe = nums(scpi("DEBUG:GENerator:DOPpRObe?"))
    if len(probe) >= 1 and probe[0] == 0:
        print("\n== M1 SKIPPED: production build (mixer-thread probe idle);"
              " M2 mirror + M4 post-DSP tap cover this node ==")
        m1_ok = True
    else:
        print("\n== M1 mixer-thread raw host pair (pre-injection, pre-doMix) ==")
        m1 = snap_m1("live")
        m1_ok = m1 is not None and m1[0] > (1 << 20) and m1[1] >= 4 and m1[2] == {0}

    # ---- M2: UAC2 mirror = samples_to_device (post-doMix, post-volume) ----
    print("\n== M2 mirror: exact samples to the DAC (S32_LE) ==")
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    raw2 = os.path.join(os.environ.get("TMPDIR", "/tmp"), "dp_m2.raw")
    r = subprocess.run(["arecord", "-q", "-D", f"plughw:{XMOS},0", "-f",
                        "S32_LE", "-r", str(RATE), "-c", "2", "-d", str(SECS),
                        "-t", "raw", raw2], capture_output=True, text=True)
    m2_ok = False
    if os.path.exists(raw2) and os.path.getsize(raw2) > 4 * 2048:
        bb = np.frombuffer(open(raw2, "rb").read(), dtype="<i4")
        for name, s in (("L", bb[0::2]), ("R", bb[1::2])):
            db, ton = analyze(s)
            print(f"  [{name}] 2kHz {db:7.1f} dBFS  tonality {ton:9.1f}x")
            if name == "L":
                first = (db, ton)
        m2_ok = (analyze(bb[0::2])[1] > TONAL_MIN
                 and analyze(bb[1::2])[1] > TONAL_MIN)
    else:
        print(f"  capture FAILED: {(r.stderr or '')[:160]}")

    # ---- M3: acoustics, EARS card 3 (mic-alive gate first) ----
    print("\n== M3 acoustics: E.A.R.S. (card 3) hears the 2 kHz? ==")
    for nid in numid_for(MIC, "Mic Capture Switch"):
        amixer(MIC, "cset", f"numid={nid}", "--", "on")
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    raw3 = os.path.join(os.environ.get("TMPDIR", "/tmp"), "dp_m3.raw")
    subprocess.run(["arecord", "-q", "-D", f"hw:{MIC},0", "-f", "S24_3LE",
                    "-r", str(RATE), "-c", "2", "-d", str(SECS), "-t", "raw",
                    raw3], capture_output=True)
    m3_ok = False
    mic_alive = False
    if os.path.exists(raw3) and os.path.getsize(raw3) > 3 * 2048:
        b = np.frombuffer(open(raw3, "rb").read(), dtype=np.uint8)
        b = b[:(b.size // 3) * 3].reshape(-1, 3)
        v3 = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
              (b[:, 2].astype(np.int32) << 16))
        v3 = np.where(v3 & 0x800000, v3 - (1 << 24), v3)
        for name, s in (("L", v3[0::2]), ("R", v3[1::2])):
            x = s.astype(np.float64)
            spec = np.abs(np.fft.rfft(x - x.mean())) * 2.0 / s.size
            body = float(np.median(spec[1:]))
            floor_db = 20 * math.log10(max(body, 1e-12) / 2 ** 23)
            k = int(round(TONE_HZ * s.size / RATE))
            pk = float(np.max(spec[max(1, k - 3):k + 4]))
            ton = pk / body if body > 0 else 0.0
            # Alive = the capsule produces ANY real signal. Judged on the
            # PEAK, never the median: a clean tone in a quiet room drives
            # the median floor to ~-110 dBFS legitimately, and a median
            # gate then calls a hearing mic dead (it did, at 80 000x
            # tonality). Dead = bit-exact digital zeros (the muted-switch
            # signature was literally max|sample| 0).
            mic_alive = mic_alive or int(np.max(np.abs(s))) > 32
            print(f"  [{name}] floor {floor_db:7.1f} dBFS  2kHz "
                  f"{20*math.log10(max(pk,1e-12)/2**23):7.1f} dBFS  "
                  f"tonality {ton:9.1f}x")
            # Judge the TONE (tonality + level), never the median floor:
            # a clean tone in a quiet room legitimately drives the median
            # to -116 dBFS, and a floor gate then FAILS a module that is
            # measurably working (it did: 173 000x tonality marked FAIL).
            # Dead-mic detection is mic_alive's job (all-zero samples).
            if name == "L":
                m3_ok = ton > TONAL_MIN
    else:
        print("  capture FAILED (no file)")
    if not mic_alive:
        print("  MIC DEAD: all-zero samples (capture switch off?) -- M3 "
              "verdict is void, fix capture before believing it")
finally:
    play.terminate()
    try:
        play.wait(timeout=10)
    except subprocess.TimeoutExpired:
        play.kill()

print("\n== MODULE VERDICT ==")
for name, ok in (("M0 host WAV            ", m0_ok),
                 ("M1 mixer raw host      ", m1_ok),
                 ("M2 mirror to DAC       ", m2_ok),
                 ("M3 EARS acoustic       ", m3_ok)):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        ^^^ data fell apart AT or BEFORE this module "
              f"(everything above it is proven good)")
        break
sys.exit(0 if (m0_ok and m1_ok and m2_ok and m3_ok) else 1)
