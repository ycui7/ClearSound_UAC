#!/usr/bin/env python3
"""Which channel map does each DSP preset present?

repeat_map.py confirmed the inversion 4/4 at 60-62 dB isolation, so it is a
property of the board, not of the rig. Before blaming wiring, test the one
variable that is cheap to change and known to alter the audio path: the DSP
preset. This build was compiled with -DCSD_DSP_PRESET_DEFAULT=2, so the
running filter is preset 2; if a preset's coefficient set merges or swaps
channels, the map will change when the preset changes.

Three outcomes, each actionable:
  - some preset maps correctly            -> it is the preset's coefficients;
                                             compare that preset's table with
                                             the one in use.
  - every preset inverts identically      -> the DSP is not responsible; look
                                             at I2S slot assignment, then at
                                             the analog path.
  - map changes with no pattern           -> the preset is not reaching the
                                             audio thread; the register
                                             read-back would still lie, which
                                             is why it is printed per rep.

Stability is judged on the no-stream captures only. Judging it across captures
that contain a +12 dBFS tone measures the tone's skirts, not the room, and did
in fact flag a stable rig as unstable while every signal reading agreed to
0.1 dB.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ears_matrix as m  # noqa: E402

DAC = m.find_dac_card(None)
EARSC = m.find_ears_card(None)
# Resolve the app root from this file, not an absolute path: the tree
# lives inside the firmware repo (csd-js-tester/rig/ sits next to
# csd_diag.py) and the old scratch copy must not be baked in.
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = "/tmp/preset_map"
os.makedirs(TMP, exist_ok=True)
AMP = 0.3
PRESETS = (0, 1, 2)
print(f"DAC {DAC}  probe {EARSC}  presets {PRESETS}")


def scpi(*cmds):
    r = subprocess.run([f"{APP}/.venv/bin/python", "csd_diag.py",
                        "--backend", "rawmidi", "--timeout", "5", *cmds],
                       capture_output=True, text=True, cwd=APP)
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def capture(stim, tag, secs=3):
    cap = f"{TMP}/{tag}.wav"
    if os.path.exists(cap):
        os.remove(cap)
    p = subprocess.Popen(["arecord", "-D", f"hw:{EARSC},0", "-f", "S24_3LE",
                          "-r", "48000", "-c", "2", "-d", str(secs), "-t", "wav", cap],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    play = None
    if stim:
        play = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error",
                                 "-re", "-i", stim, "-f", "alsa", "-ac", "2",
                                 f"plughw:{DAC},0"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.wait()
    if play:
        play.wait()
    time.sleep(0.4)
    return m.analyse(cap, tones=(m.TONE_HZ,))


# Full scale on both controls, verified: an unverified volume write is what made
# the earlier per-channel volume cases meaningless.
subprocess.run(["amixer", "-c", str(DAC), "cset", "numid=5", "--", "192,192"],
               capture_output=True)
subprocess.run(["amixer", "-c", str(DAC), "cset", "numid=6", "--", "192"],
               capture_output=True)
time.sleep(0.5)
v5 = subprocess.run(["amixer", "-c", str(DAC), "cget", "numid=5"],
                    capture_output=True, text=True).stdout
if "values=192,192" not in v5:
    raise SystemExit(f"volume did not land: {v5.strip().splitlines()[-2:]}")

STIM = {}
for tag, al, ar in (("none", 0.0, 0.0), ("L", AMP, 0.0), ("R", 0.0, AMP)):
    p = f"{TMP}/{tag}.wav"
    m.write_tone24(p, 3.0, al, ar)
    STIM[tag] = p

baseline_floors = []
for preset in PRESETS:
    setres = scpi(f"DIAG:DSP:PRESet {preset}")
    readback = scpi("DIAG:DSP:PRESet?")
    print(f"\n=== preset {preset}  (set reply={setres} read-back={readback})")
    # Settle: the preset is adopted by the audio thread on a frame boundary.
    time.sleep(1.0)
    for tag in ("none", "L", "R"):
        a = capture(None if tag == "none" else STIM[tag], f"p{preset}_{tag}")
        tl, tr = a["L"].tone_db[m.TONE_HZ], a["R"].tone_db[m.TONE_HZ]
        if tag == "none":
            fl = max(a["L"].floor, a["R"].floor)
            baseline_floors.append(fl)
            print(f"  no stream   cavityL {tl:+7.1f} cavityR {tr:+7.1f}  floor {fl:+6.1f}")
        else:
            env = baseline_floors[-1] if baseline_floors else -69.0
            loud = "R" if tr > tl else "L"
            iso = abs(tr - tl)
            aud = max(tl, tr) > env + 12
            # Which cavity the stimulus reached, and whether that is the
            # expected one. Getting this comparison backwards made an inverted
            # map print "MATCHED" -- the label has to follow the physical
            # question "did USB L land in cavity L", not its negation.
            reached = loud if aud else "none"
            label = ("MATCHED" if reached == tag else
                     "INVERTED" if reached != "none" else "NO-OUTPUT")
            print(f"  USB {tag} only  cavityL {tl:+7.1f} cavityR {tr:+7.1f}   "
                  f"-> reached cavity {reached} (isolation {iso:.0f} dB)  {label}")

print("\n=== stability, judged on no-stream captures only ===")
if baseline_floors:
    spread = max(baseline_floors) - min(baseline_floors)
    print(f"  no-stream floors: {[f'{f:+.1f}' for f in baseline_floors]}  spread {spread:.1f} dB")
    if spread > 4:
        print("  RIG UNSTABLE -- do not conclude anything about the board.")
    else:
        print("  rig stable; the channel map above is a property of the board.")
