#!/usr/bin/env python3
"""Is the L/R swap reproducible, or is the rig unstable?

The first clean measurement (rig_probe.py, writer verified against its own
file) put a USB-left tone into cavity R at +21.7 vs -40.8 in cavity L -- 62 dB
of isolation, not something to explain away. A later run of a different script
found NO tone in either cavity from the same single-channel stimulus, and a
noise floor that sat 20 dB higher during one case than the next.

Both cannot be trusted. So: repeat the same measurement N times inside one
loaded session, with a no-stream baseline between reps. A result that appears
in every rep is a property of the board. A result that appears once is a
property of the rig, and the rig gets fixed before any hardware conclusion.

The floor is printed alongside every reading precisely so that a run where the
environment moved is visible rather than silently averaged.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ears_matrix as m  # noqa: E402

DAC = m.find_dac_card(None)
EARSC = m.find_ears_card(None)
TMP = "/tmp/repeat_map"
os.makedirs(TMP, exist_ok=True)
REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
AMP = 0.3
print(f"DAC {DAC}  probe {EARSC}  reps {REPS}  amp {AMP}")


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
    time.sleep(0.4)                      # let the stream tear down cleanly
    return m.analyse(cap, tones=(m.TONE_HZ,))


# Full scale on both volume controls, verified. If a control is stuck, that is
# the first thing to know: the factory image was seen ignoring cset entirely.
for n in (5, 6):
    subprocess.run(["amixer", "-c", str(DAC), "cset", f"numid={n}", "--",
                    "192,192" if n == 5 else "192"], capture_output=True)
time.sleep(0.5)
print("volume read-back:",
      subprocess.run(["amixer", "-c", str(DAC), "cget", "numid=5"],
                     capture_output=True, text=True).stdout.split("values=")[-1].strip().splitlines()[0],
      "/",
      subprocess.run(["amixer", "-c", str(DAC), "cget", "numid=6"],
                     capture_output=True, text=True).stdout.split("values=")[-1].strip().splitlines()[0])

STIM = {}
for tag, al, ar in (("none", 0.0, 0.0), ("L", AMP, 0.0), ("R", 0.0, AMP),
                    ("both", AMP, AMP)):
    p = f"{TMP}/{tag}.wav"
    m.write_tone24(p, 3.0, al, ar)
    STIM[tag] = p

# Summary table: (rep, tag) -> (toneL, toneR, floor)
rows = {}
for rep in range(1, REPS + 1):
    for tag in ("none", "L", "R", "both"):
        a = capture(None if tag == "none" else STIM[tag], f"r{rep}_{tag}")
        tl, tr = a["L"].tone_db[m.TONE_HZ], a["R"].tone_db[m.TONE_HZ]
        fl = max(a["L"].floor, a["R"].floor)
        rows[(rep, tag)] = (tl, tr, fl)
        verdict = ""
        if tag in ("L", "R"):
            loud = "R" if tr > tl else "L"
            iso = abs(tr - tl)
            audible = max(tl, tr) > fl + 12
            verdict = (f"-> cavity {loud} {'AUDIBLE' if audible else 'NOT AUDIBLE'}"
                       f" (isolation {iso:.0f} dB)")
        print(f"  rep{rep} {tag:5s} cavityL {tl:+7.1f}  cavityR {tr:+7.1f}   floor {fl:+6.1f}  {verdict}")

print("\n=== consistency ===")
# Judge stability on the no-stream captures ONLY. Taking the floor across
# captures that contain a +12 dBFS tone measures the tone's own skirts, not the
# environment: it reported a perfectly stable rig as "UNSTABLE, spread 18.7 dB"
# in a run where all four reps agreed on the channel map to 0.1 dB.
floors = [rows[(r, "none")][2] for r in range(1, REPS + 1)]
print(f"  no-stream floors: {[f'{f:+.1f}' for f in floors]}  "
      f"spread {max(floors)-min(floors):.1f} dB")
if max(floors) - min(floors) > 4:
    print("  RIG UNSTABLE: the environment moved more than 4 dB between baseline")
    print("  captures. Do not conclude anything about the board from this run.")

swaps = []
for rep in range(1, REPS + 1):
    tlL, trL, _ = rows[(rep, "L")]
    tlR, trR, _ = rows[(rep, "R")]
    okL = max(tlL, trL) > rows[(rep, "none")][2] + 12
    okR = max(tlR, trR) > rows[(rep, "none")][2] + 12
    if okL and okR:
        mapL = "R" if trL > tlL else "L"     # USB L lands in cavity mapL
        mapR = "L" if tlR > trR else "R"     # USB R lands in cavity mapR
        swaps.append((rep, f"USB L->{mapL}  USB R->{mapR}",
                      "SWAPPED" if (mapL, mapR) == ("R", "L") else
                      "MATCHED" if (mapL, mapR) == ("L", "R") else "AMBIGUOUS"))
    else:
        swaps.append((rep, "no usable response to a single-channel stimulus", "NO-RESPONSE"))
for rep, desc, tag in swaps:
    print(f"  rep{rep}: {desc:46s} {tag}")
concl = {t for _, _, t in swaps}
print(f"\n  consistent across reps: {concl}")
print("  A single-channel stimulus that produces no output at all is itself a"
      " finding -- but only once the rig is shown stable.")
