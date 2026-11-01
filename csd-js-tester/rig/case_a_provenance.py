#!/usr/bin/env python3
"""Provenance of the case-A tone, one dial the firmware already owns: the
codec SYSTEM MUTE (reg 7 bit0). Write it over the proven quoted SCPI path
(reg_write_smoke proved WRITE lands and reads back) during the exact burst
window ears case A measures (arecord starts, ffmpeg zeros join 0.7 s in).

  tone vanishes with reg7 muted   -> it comes from the board's analog
                                    output stage -> firmware owns the fix,
                                    and the automute/mute gate experiment
                                    (rig/automute_ab.py with burst timing)
                                    is the implementation to land.
  tone stays -45 dBFS with the    -> it does NOT come from the codec.
  codec muted                        ~4000 counts of 24-bit at the capsule
                                     would have to arrive acoustically or
                                     electrically from elsewhere. That is
                                     measurement provenance, stated with
                                     the numbers, and NO firmware change
                                     can remove what the DAC is not making
                                     -- the same conclusion the four-tap
                                     digital audit already forced from the
                                     sample side.

Second leg: automute ON (reg2=0x54/reg4=128, proven-asserted on zeros last
run) with the SAME burst-window capture -- the cell the 4 s-delayed A/B
missed. If the codec idles into automute during zeros, the artifact level
must collapse with it. Registers are snapshotted before and restored after,
one by one, read-verified: experiment on silicon, leave silicon as found.
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
SECS = 3
REG7_UNMUTED = 0x00        # FILTER_SHAPE(0)|BYPASS_OSF(0)|UNMUTE(0) == boot 0x07
REG7_MUTED = 0x01
SNAP_ADDRS = [0x02, 0x04, 0x05, 0x07]


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


def wr_verified(a, v):
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
    sys.exit("snapshot failed -- refusing to experiment blind")
SNAP = dict(zip(SNAP_ADDRS, snap))
print("snapshot:", {f"0x{a:02X}": f"0x{v:02X}" for a, v in SNAP.items()})

for line in subprocess.run(["amixer", "-c", str(MIC), "controls"],
                           capture_output=True, text=True).stdout.splitlines():
    if "Mic Capture Switch" in line and line.startswith("numid="):
        amixer(MIC, "cset", line.split(",", 1)[0].split("=")[1], "--", "on")
for nid in (5, 6):
    amixer(XMOS, "cset", f"numid={nid}", "0,0")
    time.sleep(0.1)
    amixer(XMOS, "cset", f"numid={nid}", "192,192")
scpi("GENerator:OFF")

Z = os.path.join(os.environ.get("TMPDIR", "/tmp"), "prov_zeros.wav")
n = int(RATE * 10)
with wave.open(Z, "wb") as w:
    w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
    w.writeframes(b"\x00" * (3 * 2 * n))


def burst_capture(tag):
    """ears case-A geometry: arecord opens first, zeros join 0.7 s in."""
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    raw = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"prov_{tag}.raw")
    if os.path.exists(raw):
        os.remove(raw)
    cap = subprocess.Popen(
        ["arecord", "-q", "-D", f"hw:{MIC},0", "-f", "S24_3LE", "-r",
         str(RATE), "-c", "2", "-d", str(SECS), "-t", "raw", raw],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.7)
    ff = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-t", str(SECS - 0.5), "-i", Z, "-f", "alsa", "-ac", "2",
         f"plughw:{XMOS},0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cap.wait(timeout=SECS + 8)
    ff.wait(timeout=8)
    b = np.frombuffer(open(raw, "rb").read(), dtype=np.uint8)
    b = b[:(b.size // 3) * 3].reshape(-1, 3)
    v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
         (b[:, 2].astype(np.int32) << 16))
    v = np.where(v & 0x800000, v - (1 << 24), v)
    out = {}
    for name, s in (("L", v[0::2]), ("R", v[1::2])):
        if int(np.max(np.abs(s))) < 32:
            print(f"  MIC DEAD ({name}) -- void"); sys.exit(3)
        x = s.astype(np.float64)
        spec = np.abs(np.fft.rfft(x - x.mean())) * 2.0 / x.size
        k = int(round(TONE_HZ * x.size / RATE))
        pk = float(np.max(spec[max(1, k - 3):k + 4]))
        out[name] = 20 * math.log10(max(pk, 1e-12) / 2 ** 23)
    return out


def show(tag, d):
    print(f"  [{tag:16s}] 1kHz L {d['L']:+7.1f}  R {d['R']:+7.1f} dBFS")


print("\n== leg 1: system mute OUT vs IN during the burst ==")
base = burst_capture("unmute")
show("zeros, unmuted", base)
if not wr_verified(0x07, REG7_MUTED):
    sys.exit("could not assert system mute -- instrument broken")
st = rd([0x48]) or []
muted_now = burst_capture("mute")
show("zeros, MUTED", muted_now)
drop = max(base["L"], base["R"]) - max(muted_now["L"], muted_now["R"])
wr_verified(0x07, SNAP[0x07])
print(f"  drop with codec muted: {drop:+.1f} dB  (0x48 during={st})")

print("\n== leg 2: automute ON, same burst geometry ==")
ok = all(wr_verified(a, v) for a, v in
         {0x02: 0x54, 0x04: 128, 0x05: 104}.items())
if not ok:
    sys.exit("automute registers did not stick -- abort")
am = burst_capture("automute")
show("zeros, automute", am)
sl = sr = False
for _ in range(15):
    v = (rd([0x48]) or [0])[0]
    sl = sl or bool(v & 0x10); sr = sr or bool(v & 0x20)
    time.sleep(0.1)
print(f"  automute asserted during window: L={sl} R={sr}")
drop2 = max(base["L"], base["R"]) - max(am["L"], am["R"])
print(f"  drop with automute: {drop2:+.1f} dB")

print("\nrestore snapshot:")
for a in sorted(SNAP):
    good = wr_verified(a, SNAP[a])
    print(f"  0x{a:02X} -> {SNAP[a]:#04x} {'OK' if good else 'FAIL'}")

print("\n== verdict ==")
if drop > 20:
    print(f"  SYSTEM MUTE KILLS IT ({drop:.0f} dB): artifact is generated at/"
          "after the codec analog stage. Firmware gate across the idle-"
          "window is the fix path.")
elif drop2 > 20 and sl and sr:
    print(f"  automute kills it ({drop2:.0f} dB) while system mute does not:"
          " gate via automute config (reg2/4/5 at boot, values proven).")
else:
    print(f"  neither system mute ({drop:+.1f} dB) nor automute ({drop2:+.1f} dB,"
          f" asserted={sl and sr}) moves it: the -45 dBFS tone is NOT made by"
          " the codec at all -- four digital taps (<=104 LSB) plus a muted-"
          "and-still-audible analog stage leave no firmware-side generator."
          " Stated as measurement, not blame: the level exists at the"
          " capsule and the board is ruled out as its source.")
