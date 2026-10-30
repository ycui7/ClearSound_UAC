#!/usr/bin/env python3
"""Locate the L/R swap: is it upstream of the codec or downstream of it?

Measured so far, with my stimulus writer verified against its own output:
a tone written into USB left is heard by the probe's channel R, and vice
versa, at ~61 dB isolation both ways. Isolation that large is normal for
E.A.R.S. (each capsule sits in its own acoustic cavity -- that is what it is
built for), so this is not an acoustic mirror effect, and the probe's own
channel order cannot be changed by rotating it. The swap is real.

Two candidate locations, and they need different fixes:

  (a) USB stream -> I2S slot -> codec input: firmware/routing. Fix in code.
  (b) codec output -> analog trace -> cavity: board or cable. Fix in hardware.

The discriminator is codec channel mute, because the codec knows which of its
own channels it is muting. Drive BOTH cavities with an identical tone, mute
codec channel A only, and see which cavity goes quiet. Whichever cavity dies
is the one codec channel A drives -- compare that against which cavity the USB
left channel reaches and you know which side of the codec the swap is on.

Volume writes are verified by read-back. The first attempt at this experiment
reported a mute case that had not actually muted anything: 'Playback Volume'
is exposed as ONE stereo control (values=2), and a single-value write only
moved one element while the other stayed at 192. A case whose stated volume
did not land measures nothing, so a mismatch aborts instead of printing a
number.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ears_matrix as m  # noqa: E402

DAC = m.find_dac_card(None)
EARSC = m.find_ears_card(None)
TMP = "/tmp/swap_locate"
os.makedirs(TMP, exist_ok=True)
print(f"DAC card {DAC}   probe card {EARSC}")


def controls(card):
    out = subprocess.run(["amixer", "-c", str(card), "controls"],
                         capture_output=True, text=True).stdout
    parsed = []
    for ln in out.splitlines():
        if not ln.startswith("numid="):
            continue
        numid = int(ln.split(",", 1)[0].split("=")[1])
        name = ln.split("name='")[1].rstrip("'") if "name='" in ln else "?"
        info = subprocess.run(["amixer", "-c", str(card), "cget", f"numid={numid}"],
                              capture_output=True, text=True).stdout
        nvals = 1
        for l2 in info.splitlines():
            if "values=" in l2 and "type=" not in l2:
                try:
                    nvals = len(l2.split("values=")[1].strip().split(","))
                except IndexError:
                    pass
        parsed.append({"numid": numid, "name": name, "nvals": nvals})
    return parsed


def cget(card, numid):
    info = subprocess.run(["amixer", "-c", str(card), "cget", f"numid={numid}"],
                          capture_output=True, text=True).stdout
    for ln in info.splitlines():
        if ln.strip().startswith(": values="):
            return ln.split("values=")[1].strip()
    return None


def cset(card, numid, vals):
    """Write exactly as many values as the control has elements."""
    n = cget(card, numid)
    nvals = len(n.split(",")) if n else 1
    if isinstance(vals, int):
        vals = [vals] * nvals
    if len(vals) != nvals:
        raise SystemExit(f"numid={numid} has {nvals} elements; asked to write {vals}")
    payload = ",".join(str(v) for v in vals)
    r = subprocess.run(["amixer", "-c", str(card), "cset", f"numid={numid}",
                        "--", payload], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"cset numid={numid} {payload}: {r.stderr.strip()[:150]}")
    time.sleep(0.3)
    got = cget(card, numid)
    if got != payload:
        raise SystemExit(f"cset numid={numid}: asked {payload}, read back {got!r} "
                         "-- aborting rather than measuring a fiction")
    return payload


def capture(stim, secs=3, tag="cap"):
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
    a = m.analyse(cap, tones=(m.TONE_HZ,))
    return a["L"].tone_db[m.TONE_HZ], a["R"].tone_db[m.TONE_HZ], a["L"].floor


print("=== volume controls on the DAC card, as the descriptor exposes them ===")
vols = [c for c in controls(DAC) if "Playback Volume" in c["name"]]
for c in vols:
    print(f"  numid={c['numid']} '{c['name']}' elements={c['nvals']} "
          f"current={cget(DAC, c['numid'])}")
if len(vols) < 2:
    raise SystemExit("expected a stereo control plus the index=1 control")
stereo = next((c for c in vols if c["nvals"] == 2), None)
if stereo is None:
    raise SystemExit("no stereo Playback Volume control found -- the per-channel "
                     "experiment is impossible as designed; stop and re-read the "
                     "descriptor instead of guessing which control is which")

# The second control must be pinned to full scale or it gates every row.
others = [c["numid"] for c in vols if c["numid"] != stereo["numid"]]
for n in others:
    print(f"  pinning numid={n} to full scale so it cannot gate the result")
    cset(DAC, n, 192)

print("\n=== 1. acoustic map: which cavity hears USB left ===")
both = f"{TMP}/both.wav"
m.write_tone24(both, 3.0, 0.3, 0.3)          # 0.3 keeps the capsule out of overload
for tag, al, ar in (("USB L only", 0.3, 0.0), ("USB R only", 0.0, 0.3)):
    s = f"{TMP}/{tag.replace(' ','_')}.wav"
    m.write_tone24(s, 3.0, al, ar)
    tl, tr, fl = capture(s, tag=tag.replace(" ", "_"))
    print(f"  {tag:12s} cavityL {tl:+6.1f}  cavityR {tr:+6.1f} dBFS  (floor {fl:+.1f})")

print("\n=== 2. codec map: which cavity does codec channel A drive ===")
for tag, pair in (("codec L=192 R=192", (192, 192)),
                  ("codec L=0   R=192", (0, 192)),
                  ("codec L=192 R=0", (192, 0))):
    landed = cset(DAC, stereo["numid"], list(pair))
    tl, tr, fl = capture(both, tag=tag.replace(" ", "_"))
    loud = "L" if tl > tr else "R"
    print(f"  {tag:20s} (read back {landed:>7s})  cavityL {tl:+6.1f}  cavityR {tr:+6.1f}"
          f"   -> still playing on cavity {loud}")

print("\n=== 3. conclusion ===")
print("  Compare:")
print("    row 'USB L only'  -> the cavity that plays is the USB-left destination")
print("    row 'codec L=0'   -> the cavity that GOES QUIET is codec channel A's")
print("  If codec channel A drives the SAME cavity that USB left reaches, the")
print("  swap is upstream of the codec (USB stream -> I2S slot assignment: look")
print("  in the audiohub routing / csd_dsp frame packing).  If it drives the")
print("  OPPOSITE cavity, the swap is downstream of the codec (analog trace or")
print("  the cable feeding the rig) and firmware will not fix it.")
