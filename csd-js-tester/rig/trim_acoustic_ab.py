#!/usr/bin/env python3
"""L/R trim proof at the output (TODO 3+4): microphone A/B, interleaved.

WHY THE MICROPHONE AND NOT THE PEAK INSTRUMENTS
The bench peak instrument (DEBUG:MIXer:DACPeak?) cannot answer this. In this
configuration it reports node 0 (host) peaking 0x96969696 while the host
streamed ZEROS -- that word is the decouple pre-fill / token signature this
same file's comments describe -- and nodes 1/2/3 read 0xFFFFFFFE (= |2|)
identically in every leg, trim or no trim. Frozen values agree with each
other, so a "node2 == node1 at unity" pass is two frozen numbers agreeing:
vacuous, not evidence. The peak instrument was tried first precisely to avoid
the room; when it cannot see the signal, the room is the honest witness.

INTERLEAVED A/B/A/B, NOT A THEN B
An earlier run measured the baseline once, then all trim legs, and every
later leg came out 3.1 dB lower than the baseline -- an unattributed step
that would have been read as a trim result. Interleaving A B A B puts the
two conditions next to each other twice, so:
  - the A pair's agreement bounds the measurement's own repeatability, and
  - a monotone drift across the four legs is visible and the verdict is
    taken from the two independent (B-A) deltas, which must agree.
Both deltas must land on 8 dB for LEFT and 0 dB for RIGHT.

Capture and FFT are the gate's own helpers (EM.record / EM.analyse). A
hand-rolled amixer call and a hand-rolled FFT have each already cost a false
result on this rig, so the proven code is reused rather than paraphrased.

Usage: python trim_acoustic_ab.py        (works on debug AND release builds)
"""
import math, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = HERE.rsplit("/rig", 1)[0]
PY = os.path.join(APP, ".venv", "bin", "python")
sys.path.insert(0, HERE)
import ears_matrix as EM
import numpy as np

RATE, TONE_HZ, SECS = 48000, 1000.0, 3
GEN_AMP = 1073741824                 # half-scale Q31, as the fingerprint rig
PEAK_FLOOR = -45.0                   # a real tone lands far above the floor
TMP = os.environ.get("TMPDIR", "/tmp")

def scpi(cmd, timeout=10):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    # csd_diag prints some replies on stderr with rc=1; reading only stdout
    # turns a good reply into an empty string that parses as a pass.
    return ((rr.stdout or "") + (rr.stderr or "")).strip()

def nums(out):
    return [int(t) for t in out.replace("\r", " ").replace(",", " ").split()]

def card(*pats):
    for line in open("/proc/asound/cards"):
        m = re.search(r"^\s*(\d+)\s*\[", line)
        if m and any(p in line.lower() for p in pats):
            return m.group(1)
    sys.exit("cards missing")

XMOS, MIC = card("xmos", "clearsound"), card("e36db", "e.a.r.s", "36db")
EM.ensure_capture_switch(int(MIC))
print(scpi("*IDN?"))

# Host silent: the generator tone is the only content, so what the mic hears
# is mix(generator) -> trim -> DAC -> room.
Z = f"{TMP}/tab_zeros.wav"
if not os.path.exists(Z):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"anullsrc=r={RATE}:cl=stereo",
                    "-t", str(SECS), Z], check=True)

def leg(tag, trim_l, trim_r):
    """One measurement at the given trim. Returns (L dBFS, R dBFS) at TONE_HZ."""
    scpi("GENerator:OFF")
    # Real SET_CUR via the gate's helper: it parks at a different value and
    # reads back, because amixer's cached value means an equal-value write
    # emits no I2C and the volume path never runs.
    EM.set_volume(int(XMOS), EM.VOL_0DB, EM.VOL_0DB, TMP)
    scpi(f"VOLume:TRIM:LEFT {trim_l}")
    scpi(f"VOLume:TRIM:RIGHT {trim_r}")
    echo = nums(scpi("VOLume:TRIM?"))
    if echo != [trim_l, trim_r]:
        raise SystemExit(f"trim echo mismatch: want [{trim_l},{trim_r}] got {echo}")
    scpi(f"GENerator:SINe {RATE},{int(TONE_HZ*1000)},{GEN_AMP}")
    out = f"{TMP}/tab_{tag}.wav"
    EM.record(int(XMOS), int(MIC), SECS, Z, out)
    scpi("GENerator:OFF")
    res = EM.analyse(out, tones=(TONE_HZ,))
    l = res["L"].tone_db[TONE_HZ]
    r = res["R"].tone_db[TONE_HZ]
    print(f"  [{tag}] trim=({trim_l},{trim_r})  L={l:+6.2f}  R={r:+6.2f} dBFS"
          f"  floor L={res['L'].floor:+.1f} R={res['R'].floor:+.1f}")
    return l, r

fails = []
def need(cond, msg):
    print(f"  {'OK ' if cond else 'BAD'} {msg}")
    if not cond:
        fails.append(msg)

# Warm-up leg, DISCARDED. The first leg after the capture switch comes in
# ~1 dB low: A1 read L=+13.92 while A2/A3/G0 -- the same trim, the same
# tone, minutes apart -- all read +14.87, and the B-pair repeated to 0.01 dB.
# That is the mic/ALSA path settling, not the firmware, and it cost the first
# run three false FAILs (delta 1 inherited A1's offset). One throwaway pass
# before the measured sequence, so the room is warm when numbers count.
print("\n== warm-up (discarded) ==")
leg("warm", 0, 0)

print("\n== A/B/A/B: trim 0/0 then LEFT -8 dB, twice ==")
a1 = leg("A1", 0, 0)
b1 = leg("B1", -64, 0)          # -64 counts x 0.125 dB = -8 dB
a2 = leg("A2", 0, 0)
b2 = leg("B2", -64, 0)

d1L, d1R = b1[0] - a1[0], b1[1] - a1[1]
d2L, d2R = b2[0] - a2[0], b2[1] - a2[1]
print(f"\n  delta 1: L {d1L:+.2f} dB   R {d1R:+.2f} dB")
print(f"  delta 2: L {d2L:+.2f} dB   R {d2R:+.2f} dB")

# repeatability of the instrument itself, before believing any delta
need(abs(a1[0] - a2[0]) <= 0.7, f"A-pair L repeatable ({abs(a1[0]-a2[0]):.2f} dB)")
need(abs(b1[0] - b2[0]) <= 0.7, f"B-pair L repeatable ({abs(b1[0]-b2[0]):.2f} dB)")
need(min(a1[0], a2[0]) > PEAK_FLOOR, "tone present above the floor (a live leg)")

for nm, dL, dR in (("delta 1", d1L, d1R), ("delta 2", d2L, d2R)):
    need(abs(dL + 8.0) <= 0.7, f"{nm}: LEFT dropped 8.0 dB (got {dL:+.2f})")
    need(abs(dR) <= 0.7,       f"{nm}: RIGHT unchanged     (got {dR:+.2f})")

print("\n== the other channel: RIGHT -8 dB, LEFT untouched ==")
c1 = leg("C1", 0, -64)
a3 = leg("A3", 0, 0)
need(abs((c1[1] - a3[1]) + 8.0) <= 0.7, f"RIGHT dropped 8 dB ({c1[1]-a3[1]:+.2f})")
need(abs(c1[0] - a3[0]) <= 0.7,        f"LEFT unchanged     ({c1[0]-a3[0]:+.2f})")

print("\n== 0.125 dB grid is real, not rounded to the codec's 0.5 dB ==")
# The ES9219Q digital volume is 0.5 dB/LSB, so a codec-applied trim could not
# produce this step at all: seeing ~0.125 dB is itself evidence the trim is
# applied at the mixer multiplier. Tolerance is the noise floor of one leg,
# so this leg reports rather than gates -- an unresolvable step is reported,
# never silently passed.
g1 = leg("G0", 0, 0)
g2 = leg("G1", -1, 0)           # one 0.125 dB step
print(f"  one step measured {g2[0]-g1[0]:+.3f} dB (target -0.125)")
if abs((g2[0] - g1[0]) + 0.125) <= 0.15:
    print("  OK  sub-codec step resolved -> applied in the multiplier, not the codec")
else:
    print("  NOTE: step not resolved above one leg's noise. Reported, not passed;"
          " the 8 dB legs above are the verdict on application.")

print("\n== contract, and rejections must be REPORTED ==")
for bad, why in ((1, "positive is above the 0 dB maximum"),
                 (-257, "below the -32 dB floor")):
    scpi("VOLume:TRIM:LEFT 0")
    before = nums(scpi("VOLume:TRIM?"))
    out = scpi(f"VOLume:TRIM:LEFT {bad}")
    errs = scpi("SYSTem:ERRor:COUNt?")
    print(f"  {bad:>5}: reply={out!r} errqueue={errs!r}")
    rejected = "error" in out.lower() or (errs.strip() not in ("0", ""))
    need(rejected, f"{bad} rejected AND reported ({why})")
    need(nums(scpi("VOLume:TRIM?")) == before, f"{bad} left the pair intact")

print(f"\n  TRIM ACOUSTIC VERDICT: {'PASS' if not fails else 'FAIL - ' + str(len(fails)) + ' leg(s)'}")
sys.exit(0 if not fails else 1)
