#!/usr/bin/env python3
"""Disambiguate the two things the first matrix run raised.

Run 1 of the acoustic matrix produced a genuinely strong acoustic response
(+26 dBFS at 1 kHz, 83 dB SNR, mic floors -69 dBFS) -- the capsules are live
and the DAC is making sound. It also produced three failures, and two of them
have one shared explanation that has to be tested before anything is fixed:

  1. STIMULUS-TO-MIC MAP. Case D (tone written to USB left) put +26.6 dBFS in
     micR and only -36.0 in micL; case E mirrored it exactly. Both channels
     then measure isolation backwards, so D and E "fail" at -62 dB.
  2. CASE F shows the same thing from the other side: dropping codec left
     volume 192 -> 64 changed the loud mic by 0.1 dB. If the loud mic is being
     driven by the USB/codec RIGHT channel, that is exactly what you see -- so
     F is not evidence that volume is ignored. C (mute both, -81 dB) proves
     volume does work.
  3. CASE A's 1 kHz bin sits ~13 dB over the floor with all-zero samples. It
     reads the same (-56.3) in C, where the DAC streams a muted tone. Whether
     that is pickup from the DAC's digital path or just the rig's own noise
     shape decides whether A is a real fail.

So three independent questions, answered with measurements rather than
assumptions. The channel map matters most: a left/right swap between the USB
channel and the physical output is a real defect worth catching, but claiming
it while it is really my own WAV writer or ffmpeg remixing would be worse than
missing it. Hence the writer is verified by reading its own output back before
any conclusion is drawn from it.

Run while the firmware is loaded; it does not load anything itself.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ears_matrix as m   # noqa: E402

EARSC = m.find_ears_card(None)
DACC = m.find_dac_card(None)
TMP = "/tmp/rig_probe"
os.makedirs(TMP, exist_ok=True)
print(f"DAC card {DACC}   EARS card {EARSC}")


def rec(path, secs=2):
    if os.path.exists(path):
        os.remove(path)
    # arecord -d takes an integer number of seconds on this alsa-utils (it
    # rejects "2.5" outright), so round up rather than crash on a float.
    d = max(1, int(secs + 0.999))
    subprocess.run(["arecord", "-D", f"hw:{EARSC},0", "-f", "S24_3LE", "-r", "48000",
                    "-c", "2", "-d", str(d), "-t", "wav", path],
                   capture_output=True, timeout=d + 30)
    return m.analyse(path, tones=(m.TONE_HZ,))


def play(stim, bg=True):
    p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
                          "-i", stim, "-f", "alsa", "-ac", "2", f"plughw:{DACC},0"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not bg:
        p.wait()
    return p


def line(tag, a):
    L, R = a["L"], a["R"]
    print(f"  {tag:44s} micL {L.tone_db[m.TONE_HZ]:+6.1f}  micR {R.tone_db[m.TONE_HZ]:+6.1f}"
          f"   (floors {L.floor:+.1f}/{R.floor:+.1f})")
    return L.tone_db[m.TONE_HZ], R.tone_db[m.TONE_HZ]


print("\n=== 0. verify my own stimulus writer before trusting it ===")
# Read the generated file back through the same analyser. If the tone is in
# the left channel of the FILE when amp_l was set, the writer is correct and
# anything downstream is real. Skipping this is how a writer bug gets reported
# as a hardware channel swap.
for name, al, ar in (("L_only", 0.8, 0.0), ("R_only", 0.0, 0.8), ("both", 0.8, 0.8)):
    p = f"{TMP}/w_{name}.wav"
    m.write_tone24(p, 1.0, al, ar)
    a = m.analyse(p, tones=(m.TONE_HZ,))
    print(f"  writer {name:7s} -> file L {a['L'].tone_db[m.TONE_HZ]:+6.1f} "
          f"R {a['R'].tone_db[m.TONE_HZ]:+6.1f} dBFS")

print("\n=== 1. is the A-case 1 kHz bump rig noise or DAC pickup? ===")
# (a) nothing streaming at all -- pure rig environment
a = rec(f"{TMP}/no_stream.wav", 3)
no_s = line("no stream, DAC idle", a)
# (b) all-zero samples streamed -- the matrix's actual case A
m.write_tone24(f"{TMP}/zeros.wav", 4.0, 0.0, 0.0)
p = play(f"{TMP}/zeros.wav")
time.sleep(0.5)
a = rec(f"{TMP}/zeros_stream.wav", 3)
p.wait()
z_s = line("all-zero samples streaming", a)
# (c) real tone streamed, to show the response is orders of magnitude above both
m.write_tone24(f"{TMP}/tone.wav", 4.0, 0.8, 0.8)
p = play(f"{TMP}/tone.wav")
time.sleep(0.5)
a = rec(f"{TMP}/tone_stream.wav", 3)
p.wait()
t_s = line("tone streaming (reference)", a)
print(f"  verdict: A-case bump {max(z_s):+.1f} vs no-stream {max(no_s):+.1f} "
      f"-> {'RIG-INHERENT (no DAC involvement; A must be judged against this, not 0)' if abs(max(z_s)-max(no_s)) < 6 else 'DAC-ASSOCIATED pickup -- rig genuinely fails its control'}")

print("\n=== 2. which physical mic hears which USB channel ===")
for name, al, ar in (("tone in USB L", 0.8, 0.0), ("tone in USB R", 0.0, 0.8)):
    p_stim = f"{TMP}/ch_{name.replace(' ','_')}.wav"
    m.write_tone24(p_stim, 4.0, al, ar)
    pr = play(p_stim)
    time.sleep(0.5)
    a = rec(f"{TMP}/ch_{name.replace(' ','_')}.cap.wav", 2.5)
    pr.wait()
    tl, tr = line(name, a)
    loud = "micL" if tl > tr else "micR"
    src = "USB R" if al == 0.0 else "USB L"
    print(f"      -> {src} drives {loud}   "
          f"{'MATCHED' if (loud == 'micL' and src == 'USB L') or (loud == 'micR' and src == 'USB R') else '*** SWAPPED ***'}")

print("\n=== 3. which physical mic does each codec volume control govern ===")
# Identical tone on both channels, so the stimulus cannot bias the result.
m.write_tone24(f"{TMP}/both.wav", 5.0, 0.8, 0.8)
for tag, vl, vr in (("vol L=192 R=192", 192, 192), ("vol L=0   R=192", 0, 192),
                    ("vol L=192 R=0", 192, 0), ("vol L=64  R=192", 64, 192)):
    # vl/vr come straight from the tuple; re-parsing the label split into 3
    # tokens ("vol", "L=192", "R=192") and raised ValueError before any row
    # was measured.
    m.set_volume(DACC, vl, vr, TMP)
    pr = play(f"{TMP}/both.wav")
    time.sleep(0.5)
    a = rec(f"{TMP}/vol_{vl}_{vr}.wav", 2.5)
    pr.wait()
    line(tag, a)
print("  (compare rows: the mic that drops when one side is muted is the one")
print("   that codec channel governs -- independent of the stimulus writer)")
