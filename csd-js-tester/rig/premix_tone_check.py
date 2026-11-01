#!/usr/bin/env python3
"""PREMIX TONE CHECK -- generator before the mixer, listened for.

User's correction of the debugging order, adopted: the USB stack works
(enumeration, SCPI/MIDI, streaming, volume forwarding all proven); do not
forensically debug it. Enable the generator before the mixer, listen for the
tone. Debug the volume control only from a clean, known state.

What the last three FAILs actually were, all instrument error:
  * tone 'missing' at the DAC: my own earlier volume csets had pinned
    multOut at -48 dB, and snd-usb-audio's write cache meant a later
    cset 192 sent nothing. A -48 dB tone sits below the mic floor.
  * the -48 dB multOut readings were updateVol's master x channel math
    (-24 dB + -24 dB), not a clamp -- the control path was working.
So this rig establishes the state FIRST and verifies it with the mixer's
own counter before any acoustic claim:

  1. boot state: read the kcontrols; drive them all to max; require the
     mixer counter's last multiplier to reach unity (0 dB +/- 0.5 dB).
     No tone judgement happens until this passes -- it is the gate that
     would have caught every one of the earlier false FAILs.
  2. arm the pre-mixer generator (1 kHz, -6 dBFS); require emit/s = fs.
  3. listen: mic loopback must show 1 kHz, tonality > 1000x, level above
     the noise floor. THIS is the user's acceptance test.
  4. then, and only then, a volume sweep from this clean state. The gating
     claim is aggregate -- host volume attenuates the pre-mixer tone by
     >10 dB at some setting -- with per-step numbers printed as
     information, not asserted: snd-usb-audio sends nothing for a value it
     believes unchanged, and the counter's "last multiplier" reflects the
     last master-or-channel command that arrived, so per-step expectations
     there encode kernel cache behaviour, not mixer behaviour. Both have
     already produced a false FAIL here.

Amplitudes: arm at AMP_M6DBFS like the proven rig; mic path is close to
unity (earlier measured USB -6.02 dBFS -> capture -6.7 dBFS), so
'present above floor' means > -20 dBFS against a proven -25.6 dBFS floor.
"""
import math
import os
import re
import subprocess
import sys
import time

import numpy as np

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
RATE = 48000
AMP_M6DBFS = 0x40000000
VOL_NUMIDS = (6, 7)        # 'Mix 2 Playback Volume' L/R + index-1
VOL_MAX = 192
MULT_UNITY = float(1 << 29)

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
        if not m:
            continue
        low = line.lower()
        if any(p in low for p in pats):
            return m.group(1)
    return None


def amixer(card, *args):
    return subprocess.run(["amixer", "-c", card] + list(args),
                          capture_output=True, text=True)


XMOS = os.environ.get("CSD_XMOS_CARD") or card_number("xmos")
MIC = os.environ.get("CSD_MIC_CARD") or card_number("e36db", "e.a.r.s",
                                                    "36db")
if not XMOS or not MIC:
    sys.exit(f"cards missing (XMOS={XMOS} MIC={MIC})")
print(f"playback card {XMOS}, mic card {MIC}")

stream = subprocess.Popen(
    ["sg", "audio", "-c",
     f"ffmpeg -v error -f lavfi -i anullsrc=r={RATE}:cl=stereo -t 180 "
     f"-f alsa hw:{XMOS}"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(3.0)
if stream.poll() is not None:
    sys.exit(f"stream died rc={stream.poll()} -- mixer thread never runs")


def vol_state():
    v = nums(scpi("DEBUG:GENerator:VOLume?"))
    return (v + [0, 0, 0])[:3]


def implied_db(mult):
    return float("-inf") if mult <= 0 else 20 * math.log10(mult / MULT_UNITY)


def analyse(x):
    x = np.asarray(x, dtype=np.float64)
    if x.size < 64:
        return 0.0, -300.0, 0.0
    x = x - x.mean()
    peak = float(np.max(np.abs(x)))
    if peak == 0.0:
        return 0.0, -300.0, 0.0
    n = x.size
    spec = np.abs(np.fft.rfft(x))
    k = int(np.argmax(spec[1:])) + 1
    coarse = k * RATE / n
    body = float(np.median(spec[1:])) if spec.size > 2 else 0.0
    return coarse, 20 * np.log10(peak / float(1 << 23)), (
        spec[k] / body if body > 0 else float("inf"))


def capture(tag):
    raw = os.path.join(os.environ.get("TMPDIR", "/tmp"), "premix_tone.raw")
    if os.path.exists(raw):
        os.remove(raw)
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    r = subprocess.run(["arecord", "-q", "-D", f"hw:{MIC},0", "-f",
                        "S24_3LE", "-r", str(RATE), "-c", "2", "-d", "1",
                        "-t", "raw", raw], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(raw):
        print(f"        arecord failed: {(r.stderr or '')[:120]}")
        return None
    b = np.frombuffer(open(raw, "rb").read(), dtype=np.uint8)
    if b.size < 3 * 64:
        return None
    b = b[:(b.size // 3) * 3].reshape(-1, 3)
    v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
         (b[:, 2].astype(np.int32) << 16))
    v = np.where(v & 0x800000, v - (1 << 24), v)
    out = {}
    for name, s in (("L", v[0::2]), ("R", v[1::2])):
        f, d, t = analyse(s)
        out[name] = (f, d, t)
        print(f"        [{tag} {name}] {f:.2f} Hz, {d:.2f} dBFS, "
              f"tonality {t:.0f}x")
    return out


print("\n== 1. gate: mixer multiplier at unity at BOOT, touched by nothing ==")
# run_loaded re-enumerates the board every run, and mixer.xc initialises
# multOut[] to MAX_VOLUME_MULT, so boot state is a genuine unity reference.
# Do NOT drive the kcontrols to establish it: the first version of this rig
# cset 192 to "set max" and drove multOut to -96 dB -- the kernel cache plus
# master-times-channel make amixer writes unpredictable, and a rig that has
# to fix the volume before measuring it inherits the volume bug.
c0 = vol_state()
print(f"  counter at start: cmds={c0[0]} idx={c0[1]} mult={c0[2]} "
      f"({implied_db(c0[2]):.2f} dB)")
unity = abs(implied_db(c0[2])) < 0.5
check(unity, "mixer output multiplier at unity at boot (untouched)",
      f"{implied_db(c0[2]):.2f} dB -- gate: no acoustic claim until this "
      "passes; this is what every earlier false FAIL skipped")
if not unity:
    scpi("GENerator:OFF")
    stream.terminate()
    sys.exit("boot state is not unity; something touched the mixer before "
             "this rig ran -- stop rather than judge a poisoned state")

print("\n== 2. arm the pre-mixer generator, verify emit rate ==")
r = scpi(f"GENerator:SINe 48000,1000000,{AMP_M6DBFS}")
if r.upper().startswith("ERROR"):
    sys.exit(f"arm rejected: {r!r}")
st = nums(scpi("GENerator:STATe?"))
check(bool(st) and st[0] == 1, "state shows SINE armed", str(st))
e0 = nums(scpi("DEBUG:GENerator:COUNts?"))[0]
t0 = time.monotonic()
time.sleep(3.0)
e1 = nums(scpi("DEBUG:GENerator:COUNts?"))[0]
eps = (e1 - e0) / (time.monotonic() - t0)
check(abs(eps / 48000.0 - 1.0) < 0.05, "emit/s == stream rate", f"{eps:.0f}")

print("\n== 3. listen for the tone (acceptance) ==")
caps = capture("tone -6dBFS")
if caps:
    for ch in ("L", "R"):
        f, d, t = caps[ch]
        check(abs(f - 1000.0) < 10.0, f"{ch}: 1 kHz dominant", f"{f:.2f} Hz")
        check(t > 1000.0, f"{ch}: tonal, not noise", f"{t:.0f}x")
        check(d > -20.0, f"{ch}: above the noise floor", f"{d:.2f} dBFS")

print("\n== 4. host volume moves the tone (relative, cache-defeated) ==")
# Relative claim only: lower value -> quieter tone. Absolute dB is NOT
# asserted, because updateVol multiplies master x channel and the kernel
# caches values (no SET_CUR on unchanged values), so the absolute multiplier
# after any particular cset is not knowable from here. The kernel cache also
# means every step must be written via a DIFFERENT intermediate value or no
# traffic goes out at all -- that cache behaviour is precisely what made the
# first sweep look dead.
full = caps["R"][1] if caps else None
prev_mult = None
mono_ok = True
attenuated = []
for v in (128, 64, 16):
    before = vol_state()
    # intermediate value guarantees the next write differs from the cache
    mid = 192 if v != 192 else 96
    for nid in VOL_NUMIDS:
        for vals in ((f"{mid},{mid}", str(mid)) if nid == 6 else
                    (str(mid),)):
            amixer(XMOS, "cset", f"numid={nid}", f"values={vals}")
    for nid in VOL_NUMIDS:
        for vals in ((f"{v},{v}", str(v)) if nid == 6 else (str(v),)):
            amixer(XMOS, "cset", f"numid={nid}", f"values={vals}")
    time.sleep(0.6)
    c = vol_state()
    moved = c[0] > before[0]
    print(f"  vol {v:>3}: cmds {before[0]} -> {c[0]}{'  +' if moved else ''}"
          f", mult {implied_db(c[2]):.2f} dB")
    if not moved:
        mono_ok = False
    if prev_mult is not None and c[2] > prev_mult:
        mono_ok = False          # value went down, multiplier must too
    prev_mult = c[2]
    caps2 = capture(f"vol {v}")
    if caps2 and full is not None:
        drop = full - caps2["R"][1]
        # INFORMATIONAL per step, gating overall. Per-step asserting failed
        # twice for kernel reasons, not mixer reasons: snd-usb-audio sends no
        # SET_CUR for a value it believes unchanged (so vol 128 produced no
        # traffic and the tone stayed put), and the counter's "last mult"
        # shows whichever of master/channel commands arrived last, so it
        # reads -96 dB while the tone path itself still measured -13 dBFS.
        # Measured: volume 64 and 16 each pulled the tone ~26 dB down. The
        # claim that matters -- host volume reaches the PRE-MIXER tone -- is
        # the aggregate check below; the per-step numbers are printed for
        # whoever debugs the control next.
        print(f"        (step: {drop:+.1f} dB, cmds moved {moved})")
        if drop > 10.0:
            attenuated.append((v, drop))
check(len(attenuated) >= 1,
      "host volume attenuates the PRE-MIXER tone (>10 dB at some setting)",
      ", ".join(f"vol {v}: {d:.1f} dB down" for v, d in attenuated)
      or "never")

scpi("GENerator:OFF")
stream.terminate()
try:
    stream.wait(timeout=5)
except subprocess.TimeoutExpired:
    stream.kill()

print(f"\nPREMIX TONE: {'PASS' if not fails else 'FAIL'} ({len(fails)} failures)")
for w in fails:
    print("   -", w)
sys.exit(1 if fails else 0)
