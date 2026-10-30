#!/usr/bin/env python3
"""Two remaining questions about the ES9219Q, answered from the chip.

Q1 -- is the analog output pair swapped inside the codec?
Register 0x0B is titled ANALOG SWAP in the datasheet register map (alongside
OC_SD_EN / OC_SD_GAIN). A swap bit set there reproduces exactly what the
microphone measured -- a tone from USB left appearing at the right capsule --
while every digital stage above the codec reads correctly, which is precisely
the state this investigation is in. It has not been read until now.

Q2 -- does per-channel volume do anything?
Measured twice, from a verified write (values read back from the control):
192,192 gave +11.7/+12.1 dBFS in the two cavities; 0,192 gave -57/-56; 192,0
gave -58/-56. Either element at zero silences BOTH capsules, and an earlier
row at 64,192 (nominally -32 dB on one channel) was indistinguishable from
192,192. Per-channel volume is therefore not behaving as two independent
channels, which is a finding in its own right and also makes the volume cases
of the matrix unreliable until understood.

This run measures the channel that USB left actually drives and steps its
volume through the dB table. If the level does not track the control, volume
handling is broken independently of the swap question.

Reads are all through the proven REGister:READ? path. Nothing here writes to
the codec.
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
TMP = "/tmp/codec_final"
os.makedirs(TMP, exist_ok=True)
AMP, TONE = 0.3, 1000.0

# Register names per the ES9219 datasheet register map.
REGS = {
    0x00: "SYSTEM_REGISTERS",
    0x01: "INPUT_SELECTION",
    0x02: "MIXING_N_AUTOMUTE",
    0x03: "ANALOG_VOL_CTRL",
    0x04: "AUTOMUTE_TIME",
    0x05: "AUTOMUTE_LEVEL",
    0x07: "FILTER_SHAPE_N_SYSTEM_MUTE",
    0x0A: "MASTER_MODE_N_SYNC",
    0x0B: "ANALOG_SWAP_OC",          # <- the swap candidate
    0x0D: "THD_BYPASS_N_MONO_MODE",
    0x0F: "VOLUME_CH1",
    0x10: "VOLUME_CH2",
    0x1B: "GENERAL_CONFIG",
    0x20: "AMPLIFIER_CONFIG",
    0x21: "INTERRUPT_MASK",
    0x2D: "ANALOG_CTRL_OVERRIDE_1",
    0x2E: "ANALOG_CTRL_OVERRIDE_2",
    0x2F: "ANALOG_CTRL_OVERRIDE_3",
    0x30: "ANALOG_CTRL_SIGNALS",
    0x40: "CHIP_STATUS",
    0x48: "AUTOMUTE_STAT",
    0x4D: "LOCK_STATUS",
}


def read(addrs):
    import base64
    payload = base64.b64encode(bytes(addrs)).decode()
    r = subprocess.run([f"{APP}/.venv/bin/python", "csd_diag.py", "--backend",
                        "rawmidi", "--timeout", "8", f'REGister:READ? "{payload}"'],
                       capture_output=True, text=True, cwd=APP)
    if r.returncode:
        raise SystemExit(f"read failed: {(r.stderr or r.stdout).strip()[:200]}")
    lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    if not lines:
        raise SystemExit("empty reply")
    return list(base64.b64decode(lines[-1], validate=True))


addrs = sorted(REGS)
vals = read(addrs)
print("=== codec registers, full channel-relevant set ===")
for a, v in zip(addrs, vals):
    print(f"  0x{a:02X} {REGS[a]:26s} = 0x{v:02X} ({v:08b})")

d = dict(zip(addrs, vals))
print("\n=== decoded ===")
r0b = d.get(0x0B, 0)
# The datasheet names OC_SD_EN[7] and OC_SD_GAIN[6:5] here and titles the
# register ANALOG SWAP in the map, while older parts in this family put an
# analog_swap bit in the low nibble. Print every bit rather than asserting an
# interpretation the local header does not define.
print(f"  0x0B ANALOG_SWAP_OC = 0x{r0b:02X}   bit7={r0b>>7&1} "
      f"bit6:5={(r0b>>5)&3} bit4={(r0b>>4)&1} bit3:0={r0b&0xF:04b}")
print("      The project header defines no bit names for 0x0B, so do not guess")
print("      which bit is the swap: compare this against a board known good, or")
print("      the datasheet section for register 11, before writing to it.")
mix = d.get(0x02, 0)
print(f"  0x02 MIXING = 0x{mix:02X}  CH2_MIX_SEL[3:2]={(mix>>2)&3} "
      f"(1=Channel2, 0=Channel1)  CH1_MIX_SEL[1:0]={mix&3} "
      f"(0=Channel1, 1=Channel2)")
print(f"      -> codec maps DAC1<-CH1 and DAC2<-CH2 : "
      f"{1 if ((mix>>2)&3)==1 and (mix&3)==0 else 'NO -- NOT 1:1'}")
print(f"  0x04 AUTOMUTE_TIME = {d.get(0x04,0)}  "
      f"{'0 = automute disabled, which is why the automute flags never moved' if d.get(0x04,0)==0 else 'non-zero'}")
print(f"  0x0D MONO bit4 = {(d.get(0x0D,0)>>4)&1}   0x07 MUTE bit0 = {d.get(0x07,0)&1}")
print(f"  0x0F/0x10 codec digital volume = {d.get(0x0F,0)}/{d.get(0x10,0)}  "
      f"(0 = max on this part)")

# ---------------------------------------------------------------- Q2: volume
print("\n=== does per-channel volume track? (driving USB left only) ===")
for n, val in ((5, "192,192"), (6, "192")):
    subprocess.run(["amixer", "-c", str(DAC), "cset", f"numid={n}", "--", val],
                   capture_output=True)
time.sleep(0.5)


def play(stim):
    return subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
                             "-i", stim, "-f", "alsa", "-ac", "2", f"plughw:{DAC},0"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cap_both(stim, tag, secs=3):
    """Capture on the probe while a stimulus plays."""
    cap = f"{TMP}/{tag}.wav"
    if os.path.exists(cap):
        os.remove(cap)
    p = subprocess.Popen(["arecord", "-D", f"hw:{m.find_ears_card(None)},0",
                          "-f", "S24_3LE", "-r", "48000", "-c", "2",
                          "-d", str(secs), "-t", "wav", cap],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    pr = play(stim) if stim else None
    p.wait()
    if pr:
        pr.wait()
    time.sleep(0.4)
    a = m.analyse(cap, tones=(TONE,))
    return a["L"].tone_db[TONE], a["R"].tone_db[TONE], max(a["L"].floor, a["R"].floor)


stim = f"{TMP}/L_only.wav"
m.write_tone24(stim, 4.0, AMP, 0.0)
_, _, env = cap_both(None, "baseline")
print(f"  no-stream baseline floor: {env:+.1f} dBFS")

VOL_ROWS = ((192, 192), (128, 192), (96, 192), (64, 192), (32, 192), (0, 192))
prev = None
for vl, vr in VOL_ROWS:
    for n, val in ((5, f"{vl},{vr}"), (6, "192")):
        subprocess.run(["amixer", "-c", str(DAC), "cset", f"numid={n}", "--", val],
                       capture_output=True)
    time.sleep(0.4)
    chk = subprocess.run(["amixer", "-c", str(DAC), "cget", "numid=5"],
                         capture_output=True, text=True).stdout
    landed = [l.split("values=")[1].strip() for l in chk.splitlines()
              if l.strip().startswith(": values=")]
    if landed and landed[0] != f"{vl},{vr}":
        print(f"  vol {vl},{vr}: WRITE DID NOT LAND (holds {landed[0]}) -- row invalid")
        continue
    tl, tr, fl = cap_both(stim, f"v{vl}")
    loud = "cavityL" if tl > tr else "cavityR"
    lv = max(tl, tr)
    delta = "" if prev is None else f"  step {lv - prev:+5.1f} dB"
    print(f"  vol {vl:3d},{vr}  cavityL {tl:+6.1f}  cavityR {tr:+6.1f}   "
          f"driven={loud} level {lv:+6.1f}{delta}")
    prev = lv

print("\n  Expected for 0.25 dB steps from -48 dBFS: 192=0 dB, 128=-16 dB,")
print("  96=-24 dB, 64=-32 dB, 32=-40 dB. Rows that do not move by roughly")
print("  those amounts mean the volume control is not reaching the samples.")
print("  A row that silences BOTH capsules from a one-channel setting means the")
print("  two channels are not independent at the volume stage.")
