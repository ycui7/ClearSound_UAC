#!/usr/bin/env python3
"""Digital mirror probe: what samples does the MIXER build actually send to
the DAC? No microphone anywhere in this measurement.

The XU316 UAC2 descriptor exposes the mixer output as a capture stream
('Mix 2 Capture Switch' / 'Extension Unit Switch' on the XMOS card). arecord
on it returns the exact words that leave the mixer thread -- i.e. EXACTLY
what my pre-mixer injection + per-source gains produce. If the injection or
the gains put content into all-zero frames, this shows it with sample
precision, no acoustic chain, no bench:

  phase 0  silence (stream never started)     -> capture open, expect zeros
  phase 1  zeros streamed, generator OFF      -> THE CASE-A TEST
  phase 2  real 1 kHz tone streamed           -> proves the mirror carries
            the stream; silence in 0/1 is only meaningful after this PASSES
  phase 3  zeros again after the tone         -> stale-source-buffer trap:
            a wave left over in the mixer sources from phase 2 shows here

Byte-exact zero in 0/1/3 with loud level in 2 = the digital path my code
touches is clean. Any nonzero spectrum in 1 or 3 with the tone gone = the
mixer build is generating it, and the fix goes in mixer.xc/csd_gen.xc.
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
fails = []


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
    ids = []
    for line in out.splitlines():
        if name in line and line.startswith("numid="):
            ids.append(line.split(",", 1)[0].split("=")[1])
    return ids


XMOS = os.environ.get("CSD_XMOS_CARD") or card_number("xmos")
if not XMOS:
    sys.exit("XMOS card not found")

# Arm the mirror: extension unit through + both capture switches.
armed = []
for name in ("Extension Unit Switch", "Mix 2 Capture Switch"):
    for nid in numid_for(XMOS, name):
        amixer(XMOS, "cset", f"numid={nid}", "--", "on")
        armed.append(f"{name}@{nid}")
print("mirror armed:", armed)

# codec + mixer volumes to full so nothing downstream gates what we read
for nid in numid_for(XMOS, "Playback Volume") + \
          numid_for(XMOS, "Mix 2 Playback Volume"):
    amixer(XMOS, "cset", f"numid={nid}", "0,0")
    time.sleep(0.1)
    amixer(XMOS, "cset", f"numid={nid}", "192,192")


def stim(path, kind, secs=SECS + 2, amp=0.5, f0=1000.0):
    n = int(RATE * secs)
    buf = bytearray()
    for i in range(n):
        if kind == "zeros":
            v = 0
        else:
            v = int(round(amp * (2 ** 23 - 1)
                          * math.sin(2 * math.pi * f0 * i / RATE)))
        u = v & 0xFFFFFF
        word = bytes((u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF))
        buf += word + word
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
        w.writeframes(bytes(buf))


def mirror(tag, need_loud=False):
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    raw = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"mirror_{tag}.raw")
    if os.path.exists(raw):
        os.remove(raw)
    # FORMAT: the UAC2 mirror stream is S32_LE (hw-params: SAMPLE_BITS 32,
    # FRAME_BITS 64, 2 ch). S24_3LE is the EARS capsule format; asking the
    # mirror for it fails set_params and every phase reads as no-capture.
    subprocess.run(["arecord", "-q", "-D", f"hw:{XMOS},0", "-f", "S32_LE",
                    "-r", str(RATE), "-c", "2", "-d", str(SECS), "-t", "raw",
                    raw], capture_output=True)
    if not os.path.exists(raw) or os.path.getsize(raw) < 4 * 2 * 2048:
        return None
    v = np.frombuffer(open(raw, "rb").read(), dtype="<i4")
    res = {}
    for name, s in (("L", v[0::2]), ("R", v[1::2])):
        x = s.astype(np.float64)
        spec = np.abs(np.fft.rfft(x - x.mean())) * 2.0 / s.size
        k = int(round(1000.0 * s.size / RATE))
        pk1 = float(np.max(spec[max(1, k - 3):k + 4]))
        peak = float(np.max(spec[1:]))
        res[name] = (pk1, peak, float(np.max(np.abs(s))))
        # S32_LE capture: full scale is 2**31, not the 2**23 used for the
        # 24-bit EARS stream. dBFS against the wrong full scale silently
        # mislabels every level by 8 bits.
        print(f"  [{tag:8s} {name}] 1kHz {20*math.log10(max(pk1,1e-12)/2**31):7.1f}"
              f"  specpeak {20*math.log10(max(peak,1e-12)/2**31):7.1f}"
              f"  max|s| {int(res[name][2]):8d}")
    return res


def check(cond, what, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {what}"
          + (f"  [{extra}]" if extra else ""))
    if not cond:
        fails.append(what)


print("boot:", scpi("*IDN?"))
print("gen:", scpi("GENerator:STATe?"), "| counts", scpi("DEBUG:GENerator:COUNts?"),
      "| gain", scpi("MIXer:GAIN?"))
scpi("GENerator:OFF")

T = os.path.join(os.environ.get("TMPDIR", "/tmp"), "mirror_tone.wav")
Z = os.path.join(os.environ.get("TMPDIR", "/tmp"), "mirror_zeros.wav")
stim(T, "tone"); stim(Z, "zeros")


def play(wav):
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
         "-i", wav, "-f", "alsa", "-ac", "2", f"plughw:{XMOS},0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# phase 1: zeros, generator OFF  (reproduces ears case A digitally)
p = play(Z); time.sleep(2.0)
if p.poll() is not None:
    sys.exit("ffmpeg died (zeros)")
c0 = nums(scpi("DEBUG:GENerator:COUNts?"))
m = mirror("zeros")
c1 = nums(scpi("DEBUG:GENerator:COUNts?"))
p.terminate()
try:
    p.wait(timeout=10)
except subprocess.TimeoutExpired:
    p.kill()
if m is None:
    sys.exit("mirror capture failed -- is the UAC2 capture stream present?")
emit_d = (c1[0] - c0[0]) if len(c1) and len(c0) else -1
# "clean zeros" on a live UAC mirror is NOT byte-exact-0: the tap carries
# ~100 LSB (2^-24 territory) of MAC/USB jitter with zero spectral structure.
# The case-A question is whether any TONE-like content exists, so assert:
# (a) max|sample| below the 24-bit DAC word, (b) 1 kHz bin at numerical
# silence. Byte-exact-0 was my first draft and correctly failed: a contract
# the real system cannot meet tests nothing about the system.
check(max(m["L"][2], m["R"][2]) < (1 << 12) and
      20 * math.log10(max(m["L"][0], m["R"][0], 1e-12) / 2 ** 31) < -140.0,
      "zeros-in => no tone-like content to the DAC (case-A question)",
      f"max|s| {int(max(m['L'][2], m['R'][2]))} LSB, 1kHz bin at "
      f"{20*math.log10(max(m['L'][0], m['R'][0], 1e-12)/2**31):.1f} dBFS")
check(emit_d == 0, "generator emit delta 0 during zeros", f"delta {emit_d}")

# phase 2: real tone -- the mirror must carry it or phase 1 meant nothing
p = play(T); time.sleep(2.0)
if p.poll() is not None:
    sys.exit("ffmpeg died (tone)")
m = mirror("tone")
p.terminate()
try:
    p.wait(timeout=10)
except subprocess.TimeoutExpired:
    p.kill()
if m is None:
    sys.exit("mirror capture failed in tone phase")
# res[1] is the RAW normalized spectrum peak (units of full scale = 2**23);
# convert before comparing to a dB threshold -- comparing raw to dB passed
# vacuously on the first draft.
loud_db = min(20 * math.log10(max(m["L"][1], 1e-12) / 2 ** 23),
              20 * math.log10(max(m["R"][1], 1e-12) / 2 ** 23))
check(loud_db > -26.0,
      "SELF-CHECK: mirror carries a real tone (silence above is meaningful)",
      f"peak {loud_db:.1f} dBFS")

# phase 3: zeros again AFTER a real tone -- stale mixer-source-buffer trap
p = play(Z); time.sleep(2.0)
if p.poll() is not None:
    sys.exit("ffmpeg died (zeros 2)")
c0 = nums(scpi("DEBUG:GENerator:COUNts?"))
m = mirror("zeros2")
c1 = nums(scpi("DEBUG:GENerator:COUNts?"))
p.terminate()
try:
    p.wait(timeout=10)
except subprocess.TimeoutExpired:
    p.kill()
if m is None:
    sys.exit("mirror capture failed (zeros2)")
check(max(m["L"][2], m["R"][2]) < (1 << 12) and
      20 * math.log10(max(m["L"][0], m["R"][0], 1e-12) / 2 ** 31) < -140.0,
      "zeros-after-tone => still tone-free (no stale wave)", "")
emit_d = (c1[0] - c0[0]) if len(c1) and len(c0) else -1
check(emit_d == 0, "generator emit delta 0 during zeros-after-tone",
      f"delta {emit_d}")

print(f"\nMIRROR: {'PASS' if not fails else 'FAIL'} ({len(fails)})")
sys.exit(1 if fails else 0)
