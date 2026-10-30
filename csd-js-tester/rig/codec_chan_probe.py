#!/usr/bin/env python3
"""Which codec channel carries which USB channel -- read from the codec.

Why this exists. The acoustic matrix reproducibly shows a tone written into USB
left being heard by the RIGHT capsule (~61 dB isolation, presets 0/1/2 alike),
while the DIAG_LOOP check shows the digital path is not swapped (USB left
reaches I2S slot 0). So the inversion is somewhere after the DSP. The two
remaining candidates need different fixes, and one earlier attempt to separate
them was invalid:

  It muted "codec channel A" through amixer and both capsules went silent,
  which would have implied the codec channels are not independent. That test
  never touched the codec. The ALSA Playback Volume controls are 0..192 over
  -48..0 dB -- the XU *software* volume convention -- while this codec's
  analog volume is a 5-bit field and its only mute is a system-wide one
  (reg 0x07 bit 0). So that experiment measured the XU path, and the control
  that behaved like a master was read as if it were per-channel.

The codec answers the question itself. Register 0x48 reports per-channel state:
bit 7 overcurrent R, bit 6 overcurrent L, bit 5 AUTOMUTE_R, bit 4 AUTOMUTE_L.
Automute is content-sensitive: a channel with no signal above threshold
asserts, a driven channel releases. So feed ONE USB channel at a time and read
which automute flag releases -- that is the codec's own statement about which
of its channels the data arrived on, through a path (REGister:READ? over MIDI)
already proven on this board, with no microphone in the loop.

Reads are taken repeatedly per condition. The first version of the codec
poller declared the I2S path live from a lock bit that was already 1 at idle;
constant bits are missing evidence, so a flag that never changes across
conditions is reported as non-discriminating rather than as health.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ears_matrix as m  # noqa: E402

# Resolve the app root from this file, not an absolute path: the tree
# lives inside the firmware repo (csd-js-tester/rig/ sits next to
# csd_diag.py) and the old scratch copy must not be baked in.
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAC = m.find_dac_card(None)
EARSC = m.find_dac_card(None)   # the board exposes the capture path too
TMP = "/tmp/codec_chan"
os.makedirs(TMP, exist_ok=True)
RATE, TONE, AMP = 48000, 1000.0, 0.3

R_CHIP, R_AUTOMUTE, R_LOCK = 0x40, 0x48, 0x4D


def regs(addrs):
    import base64
    payload = base64.b64encode(bytes(addrs)).decode()
    r = subprocess.run([f"{APP}/.venv/bin/python", "csd_diag.py", "--backend",
                        "rawmidi", "--timeout", "6", f'REGister:READ? "{payload}"'],
                       capture_output=True, text=True, cwd=APP)
    if r.returncode:
        raise SystemExit(f"REGister:READ? failed: {(r.stderr or r.stdout).strip()[:200]}")
    line = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    if not line:
        raise SystemExit("REGister:READ? returned an empty reply")
    import base64 as b64
    return list(b64.b64decode(line[-1], validate=True))


def decode(v):
    """Split every field, including the per-channel automute bits.

    The first poller stored 0x48 as one integer and printed it, so the
    per-channel information that answers this exact question was discarded.
    """
    a = v[1]
    return {"chip": v[0] >> 2, "automute_agg": v[0] & 3, "lock": v[0] & 1,
            "oc_r": (a >> 7) & 1, "oc_l": (a >> 6) & 1,
            "am_r": (a >> 5) & 1, "am_l": (a >> 4) & 1,
            "lock_status": v[2], "raw": v}


def poll(n=4, gap=0.25):
    out = []
    for _ in range(n):
        try:
            out.append(decode(regs([R_CHIP, R_AUTOMUTE, R_LOCK])))
        except SystemExit:
            raise
        time.sleep(gap)
    return out


def play(stim):
    return subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
                             "-i", stim, "-f", "alsa", "-ac", "2", f"plughw:{DAC},0"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ALSA volume at full on both controls, verified: if the XU volume sat low the
# codec might never see enough signal to release automute, and a "no signal"
# reading would be a mixer artifact.
for n, val in ((5, "192,192"), (6, "192")):
    subprocess.run(["amixer", "-c", str(DAC), "cset", f"numid={n}", "--", val],
                   capture_output=True)
time.sleep(0.5)
chk = subprocess.run(["amixer", "-c", str(DAC), "cget", "numid=5"],
                     capture_output=True, text=True).stdout
print(f"DAC card {DAC}   XU volume read-back: "
      f"{[l.strip() for l in chk.splitlines() if 'values=' in l][:1]}")

CONDS = (("none", 0.0, 0.0), ("L only", AMP, 0.0), ("R only", 0.0, AMP),
         ("both", AMP, AMP))
rows = {}
for tag, al, ar in CONDS:
    stim = None
    if al or ar:
        stim = f"{TMP}/{tag.replace(' ', '_')}.wav"
        m.write_tone24(stim, 4.0, al, ar)
    p = play(stim) if stim else None
    time.sleep(1.0)
    rows[tag] = poll()
    if p:
        p.wait()
    time.sleep(0.8)
    flags = {(d["am_l"], d["am_r"]) for d in rows[tag]}
    print(f"  {tag:7s} automute L={sorted({d['am_l'] for d in rows[tag]})} "
          f"R={sorted({d['am_r'] for d in rows[tag]})}   "
          f"lock={sorted({d['lock'] for d in rows[tag]})}  "
          f"chip_id={rows[tag][0]['chip']}  raw={rows[tag][0]['raw']}")

print("\n=== interpretation ===")
base = rows["none"]
base_am = (max(d["am_l"] for d in base), max(d["am_r"] for d in base))
print(f"  baseline (nothing playing): automute L={base_am[0]} R={base_am[1]}")

usable = True
for src in ("L only", "R only"):
    rs = rows[src]
    rel_l = min(d["am_l"] for d in rs) < base_am[0]
    rel_r = min(d["am_r"] for d in rs) < base_am[1]
    which = ("channel L" if rel_l and not rel_r else
             "channel R" if rel_r and not rel_l else
             "BOTH channels" if rel_l and rel_r else "NEITHER (no release)")
    if rel_l == rel_r:
        usable = False
    print(f"  stimulus on USB {src:6s} -> automute released on codec {which}")

print()
if not usable:
    print("  NON-DISCRIMINATING: the automute flags did not separate the two")
    print("  stimuli. Either automute is disabled in the codec config, or its")
    print("  threshold is not met. Check ES9219Q_MIXING_N_AUTOMUTE_CONFIG (0x02)")
    print("  before drawing any conclusion -- and do NOT report the constant")
    print("  bits as evidence the codec is fine.")
    sys.exit(2)
print("  Compare with the digital map from rig/i2s_loop_check.py (USB L -> I2S")
print("  slot 0). If the codec receives USB L on its OWN channel L, the")
print("  inversion is downstream of the codec: output routing or the cable into")
print("  the probe. If it receives USB L on channel R, the codec is being fed")
print("  swapped slots and the fix is in the I2S slot assignment.")
