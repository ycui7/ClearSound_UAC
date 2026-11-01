#!/usr/bin/env python3
"""Case A / mute effectiveness, the simple way (user directives 2026-11-01).

Send a 1 kHz SINE through the DAC. Capture it twice: codec UNMUTED, then
codec SYSTEM MUTED (reg 7 bit 0, quoted SCPI path, read back after every
write, restored at the end). FFT both at 1 kHz and compare. The drop IS the
mute effectiveness; it also tells provenance:

  drop >= 60 dB          -> PASS: everything the mic hears comes out of the
                            codec's analog stage, and the mute kills it.
  tone survives muting   -> FAIL: that energy arrives by another path
                            (environmental / electrical) -- no firmware
                            change can remove what the DAC is not making.
  no tone when UNMUTED   -> INVALID (rc 2): the stimulus never reached the
                            DAC; a mute-vs-unmute comparison of silence
                            would prove nothing (this was the flaw in the
                            first version of this rig, which streamed
                            all-zero samples in both legs and measured only
                            environmental noise).

Same stimulus, same geometry, same volume in both legs; the mute register
is the ONLY difference.
"""
import base64, math, os, re, subprocess, sys, time, wave

import numpy as np

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
RATE, TONE_HZ, SECS = 48000, 1000.0, 3
HOST_AMP = 0.4        # of full scale, S24_3LE wav -- the rig's B-case level
REG = 0x07
PEAK = -40.0          # a live tone lands near -18 dBFS on this rig
DROP = 60.0           # dB the system mute must remove

TMP = os.environ.get("TMPDIR", "/tmp")


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


XMOS, MIC = card("xmos", "clearsound"), card("e36db", "e.a.r.s", "36db")
print(scpi("*IDN?"))

# Proven switch/volume helpers from the acoustic gate: a hand-rolled amixer
# call in the first draft failed 'Wrong control identifier' and the rig
# still LOOKED alive, so reuse the gate's code, not a paraphrase of it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ears_matrix as EM
EM.ensure_capture_switch(int(MIC))
EM.set_volume(int(XMOS), EM.VOL_0DB, EM.VOL_0DB, TMP)
scpi("GENerator:OFF")          # the sine comes from the HOST stream


def make_tone(path, amp):
    FULL = 2 ** 23 - 1
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
        buf = bytearray()
        for i in range(int(RATE * 10)):
            v = int(round(amp * FULL *
                          math.sin(2 * math.pi * TONE_HZ * i / RATE)))
            u = max(-FULL, min(FULL - 1, v)) & 0xFFFFFF
            buf += bytes((u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF)) * 2
        w.writeframes(bytes(buf))


T = os.path.join(TMP, "caseA_tone.wav")
make_tone(T, HOST_AMP)


def capture_1k(tag, muted):
    """Identical geometry every leg: mic opens, 0.7 s later the SAME sine
    file starts, and 0.5 s AFTER the stream (t=1.2 s) we set reg 7 and read
    it back. Ordering matters: the hub's bring-up runs AudioHwConfig_UnMute
    at stream START and overwrote a pre-stream mute write mid-capture (the
    first version of this rig asserted the mute BEFORE the stream and the
    drift check caught the firmware unmuting it). Setting it after the start
    makes OUR write the last one; it is checked again after settling, and
    the FFT window is the tail that follows."""
    want = 0x01 if muted else 0x00
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    raw = f"{TMP}/caseA_{tag}.raw"
    cap = subprocess.Popen(
        ["arecord", "-q", "-D", f"hw:{MIC},0", "-f", "S24_3LE", "-r",
         str(RATE), "-c", "2", "-d", str(SECS), "-t", "raw", raw],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.7)
    ff = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error",
                           "-t", str(SECS - 0.5), "-i", T, "-f", "alsa",
                           "-ac", "2", f"plughw:{XMOS},0"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)                      # stream has started; bring-up done
    if not wr(REG, want):                # our write lands LAST
        wr(REG, before)
        cap.wait(timeout=SECS + 8); ff.wait(timeout=8)
        sys.exit(f"could not set reg 7 to 0x{want:02X} (read-back failed)")
    time.sleep(0.3)
    assert rd(REG) == want, "mute state drifted after it was set"
    cap.wait(timeout=SECS + 8); ff.wait(timeout=8)
    b = np.frombuffer(open(raw, "rb").read(), dtype=np.uint8)
    b = b[:(b.size // 3) * 3].reshape(-1, 3)
    v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
         (b[:, 2].astype(np.int32) << 16))
    v = np.where(v & 0x800000, v - (1 << 24), v)
    # FFT only the TAIL: the window that starts after the mute write and
    # its re-verification -- both legs, same offsets, tone running.
    tail = int(RATE * 1.3)
    r = {}
    for name, s in (("L", v[0::2][-tail:]), ("R", v[1::2][-tail:])):
        x = s.astype(np.float64)
        spec = np.abs(np.fft.rfft(x - x.mean())) * 2.0 / x.size
        k = int(round(TONE_HZ * x.size / RATE))
        r[name] = 20 * math.log10(max(float(np.max(spec[k-3:k+4])), 1e-12) / 2**23)
    return r


before = rd(REG)
u = capture_1k("unmuted", muted=False)
m = capture_1k("muted",   muted=True)
wr(REG, before)

au, am = max(u["L"], u["R"]), max(m["L"], m["R"])
print("  host 1 kHz sine, identical stimulus in both legs:")
print(f"    unmuted : L {u['L']:+6.1f}  R {u['R']:+6.1f} dBFS")
print(f"    muted   : L {m['L']:+6.1f}  R {m['R']:+6.1f} dBFS")
print(f"    mute removes {au - am:.1f} dB")

print("\n== verdict ==")
if au < PEAK:
    print(f"  INVALID -- no tone with the codec UNMUTED ({au:+.1f} dBFS): the "
          "stimulus never reached the DAC. Fix the stream path. Comparing "
          "two silences proves nothing (that was the old flaw)."); sys.exit(2)
if au - am >= DROP:
    print(f"  PASS -- mute effective ({au - am:.1f} dB removed): the tone is "
          "made by the codec's analog stage, and muting removes it."); sys.exit(0)
print(f"  FAIL -- mute removed only {au - am:.1f} dB ({am:+.1f} dBFS survives):"
      " either the mute control is not doing its job, or that energy arrives"
      " by another path muting the codec cannot touch.")
sys.exit(1)
