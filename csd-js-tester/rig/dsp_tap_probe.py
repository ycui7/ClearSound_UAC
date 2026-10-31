#!/usr/bin/env python3
"""M4 audit: the words AFTER the DSP, pre-I2S, via the app's own DIAG_LOOP
tap on the UAC2 capture (no mic, no cable).

The module chain the user demanded, with the one node the earlier probes
did NOT cover: the UAC2 'Mix 2 Capture' mirror reads the MIXER OUTPUT node
(pre-DSP). The app's UserBufferManagement runs csd_dsp_frame() IN PLACE on
the USB->DAC buffer (the mixer's GiveSamplesToDevice output) and, under
CSD_DSP_DIAG_LOOP, mirrors those post-DSP words onto sampsFromAudioToUsb
[0..1] -- the UAC2 capture payload. So with the mixer capture switches OFF,
hw:4,0 capture streams EXACTLY the words that clock out to I2S.

Experiment (no rebuild; all via SCPI + ALSA):
  preset 2 (boot default, DSP live)  vs  preset 0 (csd_dsp_frame byte-exact
  bypass: `if (... preset == 0u) return;`). For each, stream all-zero host
  frames and inspect the capture:
     DSP emits content on zeros  -> tone bins here with preset 2, clean
                                    with preset 0. That module is where the
                                    data 'stops making sense'.
     DSP clean                   -> zeros are byte-exact at the last digital
                                    node; report it and keep walking.
  A tone-stream phase validates the tap first: a silent tap proves nothing.
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
SECS = 5
TONE_HZ = 2000.0
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


XMOS = os.environ.get("CSD_XMOS_CARD") or card_number("xmos")
if not XMOS:
    sys.exit("XMOS card missing")

print(scpi("*IDN?"))

# DIAG_LOOP tap = the normal capture payload: capture switches + extension
# units must be OFF so the XU matrix does not steer another node in.
off_ids = []
out = subprocess.run(["amixer", "-c", str(XMOS), "controls"],
                     capture_output=True, text=True).stdout
for line in out.splitlines():
    if ("Mix 2 Capture Switch" in line or "Extension Unit Switch" in line) \
            and line.startswith("numid="):
        nid = line.split(",", 1)[0].split("=")[1]
        amixer(XMOS, "cset", f"numid={nid}", "--", "off")
        off_ids.append(nid)
print("capture-steering switches forced OFF:", off_ids)

# volumes full
for nid_kw in ("Playback Volume", "Mix 2 Playback Volume"):
    for line in out.splitlines():
        if nid_kw in line and line.startswith("numid="):
            nid = line.split(",", 1)[0].split("=")[1]
            amixer(XMOS, "cset", f"numid={nid}", "0,0")
            time.sleep(0.05)
            amixer(XMOS, "cset", f"numid={nid}", "192,192")

Z = os.path.join(os.environ.get("TMPDIR", "/tmp"), "m4_zeros.wav")
T = os.path.join(os.environ.get("TMPDIR", "/tmp"), "m4_tone.wav")


def stim(path, tone):
    n = int(RATE * 8)
    buf = bytearray()
    for i in range(n):
        v = 0
        if tone:
            v = int(round(0.5 * (2 ** 23 - 1)
                          * math.sin(2 * math.pi * TONE_HZ * i / RATE)))
        u = v & 0xFFFFFF
        word = bytes((u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF))
        buf += word + word
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
        w.writeframes(bytes(buf))


stim(Z, False); stim(T, True)


def play(wav):
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
         "-i", wav, "-f", "alsa", "-ac", "2", f"plughw:{XMOS},0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def grab(tag):
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    raw = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"m4_{tag}.raw")
    if os.path.exists(raw):
        os.remove(raw)
    r = subprocess.run(["arecord", "-q", "-D", f"hw:{XMOS},0", "-f",
                        "S32_LE", "-r", str(RATE), "-c", "2", "-d", str(SECS),
                        "-t", "raw", raw], capture_output=True, text=True)
    if not os.path.exists(raw) or os.path.getsize(raw) < 4 * 2 * 2048:
        print(f"  [{tag}] CAPTURE FAILED {(r.stderr or '')[:120]}")
        return None
    v = np.frombuffer(open(raw, "rb").read(), dtype="<i4")
    res = {}
    for name, s in (("L", v[0::2]), ("R", v[1::2])):
        x = s.astype(np.float64)
        spec = np.abs(np.fft.rfft(x - x.mean())) * 2.0 / s.size
        body = float(np.median(spec[1:]))
        k = int(round(TONE_HZ * s.size / RATE))
        pk = float(np.max(spec[max(1, k - 3):k + 4]))
        mx = int(np.max(np.abs(s)))
        res[name] = (pk, mx)
        print(f"  [{tag:10s} {name}] 2kHz {20*math.log10(max(pk,1e-12)/2**31):7.1f}"
              f" dBFS  tonality {(pk/body if body>0 else 0):9.1f}x"
              f"  max|s| {mx}")
    return res


def check(cond, what, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {what}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        fails.append(what)


# ---- tap validation: real tone must be loud at the last digital node ----
scpi("DIAG:DSP:PRESet 2")
p = play(T); time.sleep(2.0)
if p.poll() is not None:
    sys.exit("ffmpeg died (tone)")
mt = grab("tone_p2")
p.terminate()
try:
    p.wait(timeout=10)
except subprocess.TimeoutExpired:
    p.kill()
if mt is None:
    sys.exit("no capture on the DIAG_LOOP tap -- cannot proceed")
tap_ok = min(mt["L"][0], mt["R"][0]) > 0.1 * (1 << 31)
check(tap_ok, "SELF-CHECK: tap carries the streamed 2 kHz (silence means nothing)",
      f"2kHz pk {20*math.log10(max(mt['L'][0],1e-12)/2**31):.1f} dBFS")
if not tap_ok:
    sys.exit("tap dead -- every zeros reading below would be vacuous")

# ---- the question: zeros at the last digital node, preset 2 vs 0 ----
for preset in (2, 0):
    st = scpi(f"DIAG:DSP:PRESet {preset}")
    back = nums(scpi("DIAG:DSP:PRESet?"))
    print(f"\n-- preset {preset} (readback {back}, set replied {st!r}) --")
    p = play(Z); time.sleep(2.0)
    if p.poll() is not None:
        sys.exit("ffmpeg died (zeros)")
    mz = grab(f"zeros_p{preset}")
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
    if mz is None:
        sys.exit("capture failed mid-run")
    quiet = max(mz["L"][0], mz["R"][0])
    content = max(quiet, 1)
    check(content < (1 << 21) and mz["L"][1] < (1 << 21) and mz["R"][1] < (1 << 21),
          f"zeros => DSP output (pre-I2S) content-free with preset {preset}",
          f"2kHz pk {20*math.log10(max(quiet,1e-12)/2**31):.1f} dBFS, "
          f"max|s| {max(mz['L'][1], mz['R'][1])}")

scpi("DIAG:DSP:PRESet 2")
print(f"\nM4 DSP-TAP: {'PASS' if not fails else 'FAIL'} ({len(fails)})")
sys.exit(1 if fails else 0)
