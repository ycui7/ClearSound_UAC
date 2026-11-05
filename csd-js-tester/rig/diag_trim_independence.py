#!/usr/bin/env python3
"""Is the L/R trim failure real, or another tester artefact?

Two ways my own test could have manufactured it:
  A. both channels moved because the FIRMWARE stored one value for both;
  B. both channels moved because the harness asked wrongly (units, or a command
     whose channel argument is ignored/ignored-by-parsing).

Decide by reading the device's own account after each single-channel write:
VOLume:TRIM? (what the firmware believes per channel) and the codec registers
(0x0F/0x10 channel volume, 0x11..0x14 master trim). Then measure acoustically
with a TONE ON ONE CHANNEL ONLY, so a crosstalk or measurement artefact cannot
masquerade as a second channel moving.
"""
import base64, os, re, subprocess, sys, time
import numpy as np

REPO = os.path.expanduser("~/sw-audio-sc/sw_usb_audio/clearsound_uac")
CLIENT = os.path.join(REPO, "csd-js-tester", "csd_diag.py")
PY, DAC, MIC = sys.executable, 3, 5


def sh(c, t=180):
    p = subprocess.run(c, capture_output=True, text=True, timeout=t)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def scpi(c, t=12):
    return sh([PY, CLIENT, "--backend", "rawmidi", "--timeout", str(t), c], t + 30)[1].strip().strip('"')


def read_regs(addrs):
    """Witness-checked register read (0x03=0x40, 0x07=0x00 must answer)."""
    ok = scpi('REGister:READ? "%s"' % base64.b64encode(bytes([0x03, 0x07] + list(addrs))).decode())
    tok = ok.split()[-1] if ok else ""
    try:
        blob = base64.b64decode(tok + "=" * ((4 - len(tok) % 4) % 4))
    except Exception:
        return None
    if len(blob) < 2 + len(addrs) or list(blob[:2]) != [0x40, 0x00]:
        return None
    return list(blob[2:])


def tone(path, secs=3.0, freq=1000.0, ch=None):
    n = int(48000 * secs)
    t = np.arange(n) / 48000.0
    x = (0.5 * 32767 * np.sin(2 * np.pi * freq * t)).astype(np.int16)
    z = np.zeros(n, dtype=np.int16)
    st = np.empty(n * 2, dtype=np.int16)
    st[0::2] = x if ch in (None, 0) else z
    st[1::2] = x if ch in (None, 1) else z
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes(st.tobytes())
    return path


def peak(path_to_play, secs=2, freq=1000.0):
    p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-i",
                          path_to_play, "-f", "alsa", "-ac", "2", "plughw:%d,0" % DAC],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    cap = "/tmp/trim_cap.wav"
    if os.path.exists(cap):
        os.remove(cap)
    sh(["arecord", "-q", "-D", "hw:%d,0" % MIC, "-f", "S24_3LE", "-r", "48000",
        "-c", "2", "-d", str(secs), "-t", "wav", cap], 90)
    p.terminate()
    try:
        p.wait(timeout=10)
    except Exception:
        p.kill()
    if not os.path.exists(cap):
        return None
    import wave
    with wave.open(cap, "rb") as w:
        n = w.getnframes()
        d = np.frombuffer(w.readframes(n), dtype=np.uint8)
    raw = d.reshape(n, 2, 3).astype(np.uint32)
    v = (raw[:, :, 0] | (raw[:, :, 1] << 8) | (raw[:, :, 2] << 16)).astype(np.int32)
    v = np.where(v & 0x800000, v - (1 << 24), v).astype(np.float64) / (1 << 23)
    out = {}
    for c in (0, 1):
        sp = np.abs(np.fft.rfft(v[:, c] * np.hanning(len(v))))
        fr = np.fft.rfftfreq(len(v), 1 / 48000.0)
        k = int(np.argmin(np.abs(fr - freq)))
        out[c] = 20 * np.log10(sp[max(1, k - 2):k + 3].max() / (len(v) / 4) + 1e-12)
    time.sleep(0.3)
    return out


def fmt(r):
    return "none" if r is None else " ".join("%02X" % b for b in r)


print("IDN:", scpi("*IDN?"))
for n in (5, 6):
    sh(["amixer", "-c", str(DAC), "cset", "numid=%d" % n, "192"])
scpi("VOLume:TRIM:LEFT 0"); scpi("VOLume:TRIM:RIGHT 0")
time.sleep(0.4)
print("\nbaseline TRIM? =", scpi("VOLume:TRIM?"),
      " regs CHvol/MASTER =", fmt(read_regs([0x0F, 0x10, 0x11, 0x12, 0x13, 0x14])))

SIX = -48   # counts of 0.125 dB = -6.000 dB
for label, cmd in (("LEFT  only -48", "VOLume:TRIM:LEFT %d" % SIX),
                   ("RIGHT only -48", "VOLume:TRIM:RIGHT %d" % SIX)):
    scpi("VOLume:TRIM:LEFT 0"); scpi("VOLume:TRIM:RIGHT 0")
    time.sleep(0.3)
    scpi(cmd)
    time.sleep(0.5)
    belief = scpi("VOLume:TRIM?")
    regs = read_regs([0x0F, 0x10, 0x11, 0x12, 0x13, 0x14])
    # per-channel tones: only the driven channel can move legitimately
    mono_L = peak(tone("/tmp/t_L.wav", 3.0, 1000.0, ch=0))
    mono_R = peak(tone("/tmp/t_R.wav", 3.0, 1000.0, ch=1))
    print("\n%s" % label)
    print("   firmware believes TRIM? = %s" % belief)
    print("   regs CHvol/MASTER       = %s" % fmt(regs))
    if mono_L and mono_R:
        print("   L-tone  -> micL %.2f micR %.2f dBFS" % (mono_L[0], mono_L[1]))
        print("   R-tone  -> micL %.2f micR %.2f dBFS" % (mono_R[0], mono_R[1]))

scpi("VOLume:TRIM:LEFT 0"); scpi("VOLume:TRIM:RIGHT 0")
print("\nrestored TRIM? =", scpi("VOLume:TRIM?"))
