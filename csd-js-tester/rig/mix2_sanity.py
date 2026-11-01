#!/usr/bin/env python3
"""Product topology check for MAX_MIX_COUNT=2 (user spec: host L+R and
generator L+R mix into DAC L/R, 4->2).

Host stream and generator are the two pre-mixer sources (the generator is
SUMMED into host slots before doMix; the default matrix at mixer.xc:1230 is
0 dB identity). Four captures, one boot, FFT at 1 kHz. The generator command
is GENerator:SINe <fs>,<freq_mHz>,<amp>[,<period_ms>] -- ONE amplitude, no
channel argument: csd_gen_fill drives both I2S channels.

  host zeros  + gen off   -> silence          (baseline)
  host zeros  + gen tone  -> tone             (generator reaches the mix)
  host tone   + gen off   -> tone             (host reaches the mix)
  host tone   + gen tone  -> tone at least as loud as either alone
                                             (both summed, neither clobbers)

Nothing else. Absence in leg 2 or 3 means that source does not ride the
matrix; a DROP in leg 4 would mean one source overwrites the other.
"""
import math, os, re, subprocess, sys, time, wave
import numpy as np

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
RATE, TONE_HZ, SECS = 48000, 1000.0, 3
GEN_AMP = 1073741824          # half scale Q31, same as the fingerprint rig
HOST_AMP = 0.4                # of full scale in the S24_3LE wav
PEAK = -40.0                  # a real tone lands far above the ~-90 floor

TMP = os.environ.get("TMPDIR", "/tmp")


def scpi(cmd, timeout=10):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def card(*pats):
    for line in open("/proc/asound/cards"):
        m = re.search(r"^\s*(\d+)\s*\[", line)
        if m and any(p in line.lower() for p in pats):
            return m.group(1)
    sys.exit("cards missing")


XMOS, MIC = card("xmos"), card("e36db", "e.a.r.s", "36db")
print(scpi("*IDN?"))

# Proven switch/volume helpers from the acoustic gate -- a hand-rolled amixer
# call in the first draft failed 'Wrong control identifier' while still
# looking plausible, so reuse the gate's code, not a paraphrase of it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ears_matrix as EM
EM.ensure_capture_switch(int(MIC))
EM.set_volume(int(XMOS), EM.VOL_0DB, EM.VOL_0DB, TMP)


def make_wav(path, amp):
    FULL = 2 ** 23 - 1
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
        buf = bytearray()
        for i in range(int(RATE * 10)):
            v = int(round(amp * FULL * math.sin(2 * math.pi * TONE_HZ * i / RATE)))
            u = max(-FULL, min(FULL - 1, v)) & 0xFFFFFF
            buf += bytes((u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF)) * 2
        w.writeframes(bytes(buf))


Z = os.path.join(TMP, "mix2_zeros.wav"); make_wav(Z, 0.0)
T = os.path.join(TMP, "mix2_tone.wav");  make_wav(T, HOST_AMP)


def capture_1k(tag, host_wav):
    """Mic opens 0.7 s BEFORE the stream (the first-open window matters),
    FFT the whole capture, report max(L,R) of the 1 kHz bin -- the same
    measurement the mute/unmute gate uses."""
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    raw = f"{TMP}/mix2_{tag}.raw"
    cap = subprocess.Popen(
        ["arecord", "-q", "-D", f"hw:{MIC},0", "-f", "S24_3LE", "-r",
         str(RATE), "-c", "2", "-d", str(SECS), "-t", "raw", raw],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.7)
    ff = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error",
                           "-t", str(SECS - 0.5), "-i", host_wav, "-f", "alsa",
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
    return max(r["L"], r["R"])


print("\n== legs (one boot) ==")
scpi("GENerator:OFF")
p1 = capture_1k("none", Z);  print(f"  host zeros + gen off  : {p1:+6.1f} dBFS")
scpi(f"GENerator:SINe {RATE},1000000,{GEN_AMP}")
p2 = capture_1k("gen", Z);   print(f"  host zeros + gen tone : {p2:+6.1f} dBFS")
scpi("GENerator:OFF")
p3 = capture_1k("host", T);  print(f"  host tone  + gen off  : {p3:+6.1f} dBFS")
scpi(f"GENerator:SINe {RATE},1000000,{GEN_AMP}")
p4 = capture_1k("both", T);  print(f"  host tone  + gen tone : {p4:+6.1f} dBFS")
scpi("GENerator:OFF")

print("\n== verdict ==")
ok = True
def need(cond, msg):
    global ok
    print(f"  {'OK ' if cond else 'BAD'} {msg}")
    ok = ok and cond
need(p1 < PEAK, f"baseline silent ({p1:+.1f})")
need(p2 >= PEAK and p2 - p1 >= 30, f"generator rides the mix ({p2:+.1f})")
need(p3 >= PEAK and p3 - p1 >= 30, f"host rides the mix ({p3:+.1f})")
need(p4 >= PEAK and p4 >= max(p2, p3) - 1.0,
     f"both summed, neither clobbers the other ({p4:+.1f} vs max {max(p2, p3):+.1f})")
print("  4->2 mix: PASS" if ok else "  4->2 mix: FAIL")
sys.exit(0 if ok else 1)
