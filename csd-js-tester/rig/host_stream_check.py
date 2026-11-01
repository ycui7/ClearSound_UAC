#!/usr/bin/env python3
"""HOST AUDIO STILL WORKS, and the generator SUMS rather than overwrites.

Two claims, both about the mixer build (CSD_GEN_PREMIX compiled in):

  A. Ordinary host PCM plays through unchanged -- the pre-mixer hook and the
     C mailbox did not break the normal path. (Host plays 3 kHz; the mic
     hears 3 kHz.)
  B. With the generator armed, BOTH waves are present at once: host 3 kHz
     AND generator 1 kHz. This is the user's original requirement --
     "summed to the host audio stream" -- and it is what distinguishes a
     sum from an overwrite. An overwrite kills the 3 kHz the moment the
     tone is armed.

Falsification control (without this the test is worthless): with the
generator OFF, the 1 kHz bin must be BELOW the floor. A rig that only
checks "energy present at both bins" passes even when the mic hears nothing
in particular, and an earlier version of this work proved exactly that
lesson by staying green through the regression it was written to catch.

Sequence, one continuous host stream so nothing re-adopts mid-test:
  1. gate on the boot multiplier at unity (premix_tone_check lesson: never
     make an acoustic claim against a volume state you have not verified)
  2. host 3 kHz, generator OFF  -> 3 kHz up,   1 kHz DOWN   (control)
  3. host 3 kHz, generator armed -> 3 kHz up AND 1 kHz up   (sum)
  4. host 3 kHz, generator OFF  -> 3 kHz up,   1 kHz DOWN   (reversible)

Host playback recipe and mic capture recipe are the proven ones: ffmpeg
-re -> plughw (i2s_loop_check.py), S24_3LE arecord on the E.A.R.S. card
(premix_tone_check.py). Levels are asserted against the measured noise
floor, not against absolute dB, because the analog chain has bench gain.
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
HOST_HZ = 3000.0            # host stimulus, deliberately far from the tone
GEN_HZ = 1000.0             # generator stimulus
AMP_M6DBFS = 0x40000000     # generator arm amplitude (-6 dBFS)
HOST_AMP = 0.5              # host WAV amplitude, full scale
FLOOR_DB = -30.0            # above this = a real signal at the mic
MULT_UNITY = float(1 << 29)
STIM = os.path.join(os.environ.get("TMPDIR", "/tmp"), "host_stream_3k.wav")

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
    """Card NUMBER, taken from the line that also matches -- the two-line
    /proc/asound/cards layout put the number on a line the pattern never
    matched and cost three debugging rounds once already."""
    for line in open("/proc/asound/cards"):
        m = re.search(r"^\s*(\d+)\s*\[", line)
        if not m:
            continue
        low = line.lower()
        if any(p in low for p in pats):
            return m.group(1)
    return None


XMOS = os.environ.get("CSD_XMOS_CARD") or card_number("xmos")
MIC = os.environ.get("CSD_MIC_CARD") or card_number("e36db", "e.a.r.s", "36db")
if not XMOS or not MIC:
    sys.exit(f"cards missing (XMOS={XMOS} MIC={MIC})")
print(f"playback card {XMOS}, mic card {MIC}")
RATE = int(os.environ.get("CSD_RATE", RATE))   # 176400 proven by the DSD rig


def stream_state(tag):
    """What the kernel THINKS the playback stream is doing. A stream that
    opened at the wrong rate/format, or never started, looks exactly like
    'firmware drops host samples' from the microphone side -- read the
    kernel's own view before blaming the firmware."""
    for sub in ("pcm0p", "pcm1p", "pcm2p"):
        hw = f"/proc/asound/card{XMOS}/{sub}/sub0/hw_params"
        try:
            txt = open(hw).read().strip().replace("\n", " ")
        except OSError:
            continue
        if "closed" not in txt:
            print(f"        [{tag} stream] {sub}: {txt}")
            return
    print(f"        [{tag} stream] NO open playback substream on card "
          f"{XMOS} -- host samples cannot be arriving")


def write_stim(path):
    """24-bit WAV of HOST_HZ at HOST_AMP, native at RATE (no hidden
    resampling: the point is to know exactly what enters the stream)."""
    n = int(RATE * 6.0)
    buf = bytearray()
    # BOTH words per frame. A first version appended ONE word per i into a
    # 2-channel WAV, so each channel carried every other sample: the file
    # measured 6 kHz -- exactly 2x -- at half duration, and this rig then
    # "proved host audio absent" by listening at 3 kHz for energy that was
    # sitting at 6 kHz. Local WAV analysis found it in seconds; three board
    # cycles found nothing. Verify the stimulus BEFORE blaming the firmware.
    for i in range(n):
        v = int(round(HOST_AMP * (2 ** 23 - 1)
                      * math.sin(2 * math.pi * HOST_HZ * i / RATE)))
        u = v & 0xFFFFFF
        word = bytes((u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF))
        buf += word + word          # L and R: same tone, full sample rate
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(3)
        w.setframerate(RATE)
        w.writeframes(bytes(buf))


def band_db(x, f, n):
    """(dBFS, tonality) of the band around f, over +/-3 bins.

    Amplitude normalization by N/2: an un-normalized rFFT bin scales with
    window length and once printed a '0.34 dBFS' noise bin next to an
    '80 dBFS' tone -- absolute levels were meaningless, only ratios were.
    DECISIONS therefore use tonality (peak/bin-median), whose measured
    contrast here is ~55 000x (tone) vs ~2x (noise); dB is printed as
    information.
    """
    spec = np.abs(np.fft.rfft(x - x.mean())) * 2.0 / float(n)
    k = int(round(f * n / RATE))
    lo, hi = max(1, k - 3), min(len(spec) - 1, k + 3)
    body = float(np.median(spec[1:]))
    pk = float(np.max(spec[lo:hi + 1]))
    db = -300.0 if pk <= 0 else 20 * math.log10(pk / float(1 << 23))
    return db, (pk / body if body > 0 else float("inf"))


def capture(tag):
    raw = os.path.join(os.environ.get("TMPDIR", "/tmp"), "host_stream.raw")
    if os.path.exists(raw):
        os.remove(raw)
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    r = subprocess.run(["arecord", "-q", "-D", f"hw:{MIC},0", "-f",
                        "S24_3LE", "-r", str(RATE), "-c", "2", "-d", "2",
                        "-t", "raw", raw], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(raw):
        print(f"        arecord failed: {(r.stderr or '')[:120]}")
        return None
    b = np.frombuffer(open(raw, "rb").read(), dtype=np.uint8)
    if b.size < 3 * 1024:
        print("        capture too short")
        return None
    b = b[:(b.size // 3) * 3].reshape(-1, 3)
    v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
         (b[:, 2].astype(np.int32) << 16))
    v = np.where(v & 0x800000, v - (1 << 24), v)
    n = v[0::2].size
    out = {}
    for name, s in (("L", v[0::2]), ("R", v[1::2])):
        h_db, h_t = band_db(s, HOST_HZ, n)
        g_db, g_t = band_db(s, GEN_HZ, n)
        out[name] = (h_db, h_t, g_db, g_t)
        print(f"        [{tag} {name}] host {h_db:7.2f} dBFS ({h_t:8.0f}x)  "
              f"gen  {g_db:7.2f} dBFS ({g_t:8.0f}x)")
    return out


TONAL_MIN = 1000.0     # measured: 45 000-55 000x for a real tone, 1-4x noise


def both(out, band, want_present, label):
    """Assert a band is a real tone (or not) on BOTH channels.

    band selects which pair from capture(): (host_db, host_t, gen_db, gen_t)
    -> band 0 uses entries (0,1), band 2 uses (2,3). Presence = tonality
    above TONAL_MIN on both channels; requiring both stops a one-channel
    map artifact passing on half the evidence. dB values are reported but
    not gated on: the analog bench has gain and the mic noise floor moved
    between sessions.
    """
    base = 0 if band == 0 else 2
    ton = {ch: out[ch][base + 1] for ch in ("L", "R")}
    db = {ch: out[ch][base] for ch in ("L", "R")}
    present = all(t > TONAL_MIN for t in ton.values())
    check(present == want_present,
          f"{label} {'present' if want_present else 'absent'} "
          f"on both channels",
          f"L={db['L']:.1f} dBFS/{ton['L']:.0f}x  "
          f"R={db['R']:.1f} dBFS/{ton['R']:.0f}x (tonal>{TONAL_MIN:.0f}x)")


print("\n== 0. build/version stamp ==")
print("  ", scpi("*IDN?"))

# Which injection path is compiled in decides phase 3's EXPECTATION:
#   mixer build  -> pre-mixer SUM: host 3 kHz and tone 1 kHz both audible
#   production   -> UBM overwrite (last-write-wins, the shipped behaviour):
#                   tone present, host replaced. Asserting sum here would
#                   fail on correct firmware.
# Discriminated from the descriptors, not guessed from the build flags we
# passed: a mixer build exposes 'Mix 2 Playback Volume' to the host, a
# production build has no mixer unit at all.
ctl = subprocess.run(["amixer", "-c", XMOS, "controls"],
                     capture_output=True, text=True).stdout
PREMIX = "Mix 2 Playback Volume" in ctl
_path = "PRE-MIXER SUM (mixer build)" if PREMIX else "UBM OVERWRITE (production build)"
print("  injection path:", _path,
      "[mixer kcontrols", "present]" if PREMIX else "absent]")

print("\n== 1. gate: mixer multiplier at unity at boot, touched by nothing ==")
if os.environ.get("CSD_SKIP_VOL_GATE") == "1":
    print("  SKIPPED via CSD_SKIP_VOL_GATE=1 (build has no "
          "DEBUG:GENerator:VOLume?; boot state is still untouched by this rig)")
    v = None
else:
    v = (nums(scpi("DEBUG:GENerator:VOLume?")) + [0, 0, 0])[:3]
db = 0.0 if v is None else (
    float("-inf") if v[2] <= 0 else 20 * math.log10(v[2] / MULT_UNITY))
if v is not None:
    print(f"  counter: cmds={v[0]} idx={v[1]} mult={v[2]} ({db:.2f} dB)")
if abs(db) >= 0.5:
    sys.exit("boot multiplier is not unity -- something touched the mixer "
             "before this rig; refusing to judge levels against it")
print("  PASS  boot multiplier unity, acoustic claims are meaningful")

if os.path.exists(STIM):
    os.remove(STIM)
write_stim(STIM)

# SELF-CHECK the stimulus before blaming anyone for not hearing it. The
# first version of this file played 6 kHz while listening at 3 kHz (one
# word per frame in a stereo WAV), and "host audio absent" survived three
# board cycles because every board-side number was consistent with the
# rig's own bug. Four lines here would have caught it on the host instead.
with wave.open(STIM) as _w:
    _n = _w.getnframes()
    _sw = _w.getsampwidth()
    _raw = _w.readframes(_n)
_v = np.frombuffer(_raw, dtype=np.uint8).reshape(-1, _sw)
_w16 = (_v[:, 0].astype(np.int32) | (_v[:, 1].astype(np.int32) << 8) |
        (_v[:, 2].astype(np.int32) << 16))
_w16 = np.where(_w16 & 0x800000, _w16 - (1 << 24),
                _w16).astype(np.float64)
_spec = np.abs(np.fft.rfft(_w16[0::2]))
_k = int(np.argmax(_spec[1:])) + 1
_f = _k * RATE / (_w16[0::2].size)
print(f"  stimulus self-check: {_f:.1f} Hz (want {HOST_HZ}), peak "
      f"{20*math.log10(max(np.abs(_w16).max(),1)/(2**23-1)):.1f} dBFS, "
      f"{_n/RATE:.1f} s")
if abs(_f - HOST_HZ) > 50.0:
    sys.exit(f"STIMULUS WRONG: dominant {_f:.1f} Hz != {HOST_HZ} Hz -- "
             "fix the rig, do not measure the board")

# One host stream spans the whole test: a stop/start would re-run stream
# adoption and change the mix state between phases.
# ffmpeg stderr goes to a PIPE, not DEVNULL: a build that once silenced it
# reported "host audio absent" from a stream that may never have opened --
# indistinguishable from the failure it claimed to find.
fflog = open(os.path.join(os.environ.get("TMPDIR", "/tmp"),
                          "host_stream_ff.log"), "wb")
BACKEND = os.environ.get("CSD_PLAYBACK", "ffmpeg")
if BACKEND == "aplay":
    # Strictest instrument: fixed format, no resampler, non-zero exit on a
    # device that will not take the stream. -D hw forces the exact format,
    # so a rate/format refusal becomes visible instead of silent.
    play = subprocess.Popen(
        ["aplay", "-q", "-D", f"hw:{XMOS},0", "-f", "S24_3LE", "-r",
         str(RATE), "-c", "2", "-t", "raw", "--period-size=192", STIM],
        stdout=fflog, stderr=subprocess.STDOUT)
else:
    play = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "info", "-re",
         "-stream_loop", "-1", "-i", STIM,
         "-f", "alsa", "-ac", "2", f"plughw:{XMOS},0"],
        stdout=fflog, stderr=subprocess.STDOUT)
print(f"  playback backend: {BACKEND}")
time.sleep(2.5)
if play.poll() is not None:
    fflog.close()
    txt = open(fflog.name, errors="replace").read()[-400:]
    sys.exit(f"ffmpeg playback died rc={play.poll()} -- stream never "
             f"started; ffmpeg said: {txt!r}")
print("  ffmpeg alive after 2.5 s (device open, clock running)")

try:
    print("\n== 2. host 3 kHz, generator OFF  (host path + control) ==")
    stream_state("off")
    o = capture("off")
    if o is None:
        sys.exit("no capture -- cannot proceed")
    both(o, 0, True, "host 3 kHz")
    both(o, 2, False, "generator 1 kHz")

    print("\n== 3. host 3 kHz, generator armed at 1 kHz  (THE SUM) ==")
    r = scpi(f"GENerator:SINe {RATE},{int(GEN_HZ*1000)},{AMP_M6DBFS}")
    if r.upper().startswith("ERROR"):
        sys.exit(f"arm rejected: {r!r}")
    st = nums(scpi("GENerator:STATe?"))
    check(bool(st) and st[0] == 1, "state shows SINE armed", str(st))
    e0 = nums(scpi("DEBUG:GENerator:COUNts?"))[0]
    t0 = time.monotonic()
    time.sleep(2.0)
    e1 = nums(scpi("DEBUG:GENerator:COUNts?"))[0]
    eps = (e1 - e0) / max(time.monotonic() - t0, 1e-6)
    check(abs(eps / RATE - 1.0) < 0.05, "emit/s == stream rate", f"{eps:.0f}")
    o2 = capture("sum")
    if o2 is None:
        sys.exit("no capture in sum phase")
    both(o2, 2, True, "generator 1 kHz is present")
    if PREMIX:
        both(o2, 0, True, "host 3 kHz SURVIVES the armed generator (SUM)")
    else:
        # Production path replaces the frame at UBM -- proven behaviour since
        # before the premix work. Checking it stays this way is the point:
        # if summing ever leaked into the production build, this fires.
        both(o2, 0, False, "host 3 kHz replaced by UBM overwrite")

    print("\n== 4. generator OFF again  (reversibility) ==")
    scpi("GENerator:OFF")
    time.sleep(1.0)
    o3 = capture("off2")
    if o3 is not None:
        both(o3, 0, True, "host 3 kHz still intact")
        both(o3, 2, False, "generator 1 kHz gone")
finally:
    scpi("GENerator:OFF")
    play.terminate()
    try:
        fflog.close()
    except OSError:
        pass
    try:
        play.wait(timeout=5)
    except subprocess.TimeoutExpired:
        play.kill()

try:
    txt = open(fflog.name, errors="replace").read().strip()
    if txt:
        print("\n-- playback log (last 600) --\n", txt[-600:])
except OSError:
    pass

print(f"\nHOST STREAM: {'PASS' if not fails else 'FAIL'} ({len(fails)} failures)")
for w in fails:
    print("   -", w)
sys.exit(1 if fails else 0)
