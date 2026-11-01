#!/usr/bin/env python3
"""Case A, the simple way (user directive 2026-11-01).

Two captures of the same all-zero stream. One with the codec UNMUTED, one
with the codec SYSTEM MUTED (reg 7 bit 0). FFT both. The 1 kHz excitation
peak must appear ONLY in the unmuted capture.

    unmuted has the peak, muted does not   -> PASS (the DAC's analog stage
                                              makes it; provenance known)
    both have it                           -> the mic/bench makes it
    neither has it                         -> artifact gone

No baselines, no floors, no drift gates. The mute register is read back
after every write, and restored.
"""
import base64, math, os, re, subprocess, sys, time, wave

import numpy as np

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
RATE, TONE_HZ, SECS = 48000, 1000.0, 3
REG, REG_UNMUTED = 0x07, 0x00


def scpi(cmd, timeout=10):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def rd(a):
    out = scpi(f'REGister:READ? "{base64.b64encode(bytes([a])).decode()}"')
    try:
        return list(base64.b64decode(out.strip().strip('"'), validate=True))[0]
    except Exception:
        return None


def wr(a, v):
    tok = base64.b64encode(bytes([a, v])).decode()
    return "ERROR" not in scpi(f'REGister:WRITE "{tok}"').upper() and rd(a) == v


def card(*pats):
    for line in open("/proc/asound/cards"):
        m = re.search(r"^\s*(\d+)\s*\[", line)
        if m and any(p in line.lower() for p in pats):
            return m.group(1)
    sys.exit("cards missing")


XMOS, MIC = card("xmos"), card("e36db", "e.a.r.s", "36db")
print(scpi("*IDN?"))

# mic capture switch on, DAC at 0 dBFS, generator off
for line in subprocess.run(["amixer", "-c", MIC, "controls"],
                           capture_output=True, text=True).stdout.splitlines():
    if "Mic Capture Switch" in line and line.startswith("numid="):
        subprocess.run(["amixer", "-q", "-c", MIC, "cset",
                        line.split(",", 1)[0].split("=")[1], "--", "on"])
for nid in (5, 6):
    subprocess.run(["amixer", "-q", "-c", XMOS, "cset", f"numid={nid}", "192,192"])
scpi("GENerator:OFF")

Z = os.path.join(os.environ.get("TMPDIR", "/tmp"), "caseA_zeros.wav")
with wave.open(Z, "wb") as w:
    w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
    w.writeframes(b"\x00" * (3 * 2 * int(RATE * 10)))


def capture_1k(tag):
    """The gate's geometry: mic opens first, zeros join 0.7 s in (the
    artifact lives in the first ~250 ms of the stream)."""
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    raw = f"{os.environ.get('TMPDIR', '/tmp')}/caseA_{tag}.raw"
    cap = subprocess.Popen(
        ["arecord", "-q", "-D", f"hw:{MIC},0", "-f", "S24_3LE", "-r",
         str(RATE), "-c", "2", "-d", str(SECS), "-t", "raw", raw],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.7)
    ff = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error",
                           "-t", str(SECS - 0.5), "-i", Z, "-f", "alsa",
                           "-ac", "2", f"plughw:{XMOS},0"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cap.wait(timeout=SECS + 8); ff.wait(timeout=8)
    b = np.frombuffer(open(raw, "rb").read(), dtype=np.uint8)
    b = b[:(b.size // 3) * 3].reshape(-1, 3)
    v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
         (b[:, 2].astype(np.int32) << 16))
    v = np.where(v & 0x800000, v - (1 << 24), v)
    r = {}
    for name, s in (("L", v[0::2]), ("R", v[1::2])):
        x = s.astype(np.float64)
        spec = np.abs(np.fft.rfft(x - x.mean())) * 2.0 / x.size
        k = int(round(TONE_HZ * x.size / RATE))
        r[name] = 20 * math.log10(max(float(np.max(spec[k-3:k+4])), 1e-12) / 2**23)
    return r


PEAK = -60.0   # anything above this at 1 kHz is "the excitation peak"
before = rd(REG)

u = capture_1k("unmuted")
print(f"  unmuted : 1kHz L {u['L']:+6.1f}  R {u['R']:+6.1f} dBFS")
if not wr(REG, 0x01):
    wr(REG, before); sys.exit("could not assert system mute (read-back failed)")
m = capture_1k("muted")
print(f"  muted   : 1kHz L {m['L']:+6.1f}  R {m['R']:+6.1f} dBFS")
wr(REG, before)

up = max(u["L"], u["R"]) >= PEAK
mp = max(m["L"], m["R"]) >= PEAK
print("\n== verdict ==")
if up and not mp:
    print(f"  PASS -- peak {max(u['L'],u['R']):+.1f} dBFS only when unmuted;"
          f" muted reads {max(m['L'],m['R']):+.1f}. The artifact is made by"
          " the codec's analog output stage, as expected."); sys.exit(0)
if up and mp:
    print("  FAIL -- peak present even with the codec muted: it is NOT from"
          " the DAC output; it is bench/mic pickup or another path."); sys.exit(1)
print(f"  PASS (no artifact) -- no peak in either condition"
      f" (unmuted {max(u['L'],u['R']):+.1f}, muted {max(m['L'],m['R']):+.1f}).")
sys.exit(0)
