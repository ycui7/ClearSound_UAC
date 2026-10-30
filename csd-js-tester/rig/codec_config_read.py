#!/usr/bin/env python3
"""Read the codec's own routing/volume config to explain the channel behaviour.

Where this leaves the investigation. Established, reproduced, rig proven stable:

  - Acoustic: a tone written to USB left is heard by the RIGHT capsule at ~61 dB
    isolation; USB right -> left capsule; identical on presets 0, 1 and 2, so
    the DSP and the preset are not responsible.
  - Digital, via the DIAG_LOOP stamp which mirrors the samples the audio thread
    is about to hand to I2S: USB left lands in capture slot 0 (+19.4 vs -31.1
    dBFS) and USB right in slot 1. Not swapped -- but this stamp reads the
    buffer BEFORE the codec, so it cannot see the codec being fed duplicated
    slots. It rules out the DSP and the routing, nothing past the I2S pins.
  - One amixer Playback Volume mute silenced BOTH capsules, which is consistent
    with the codec receiving one channel twice rather than with independent
    channels. That control is XU software volume, so it was never a codec
    channel control, and the reading is not evidence either way.
  - Automute is disabled on this part: 0x48 reads 1 at idle and 2 while
    streaming, never setting the per-channel flags at bits 4/5, so it cannot
    discriminate.

What is left is the codec's own configuration. These registers decide what each
channel plays and are read over the already-proven REGister:READ? path, with no
writes and no microphone:

  0x01 INPUT_SELECT            each channel's source; both channels set to the
                               same source IS the failure mode
  0x02 MIXING_N_AUTOMUTE       mixing would sum L into R -- and explains why
                               automute never asserts
  0x03 ANALOG_VOL_CTRL         per-channel analog enable/volume
  0x07 FILTER_SHAPE_N_SYSTEM_MUTE   master mute only; if set, explains the dead
                               factory image
  0x0D THD_BYPASS_N_MONO_MODE  bit 4 mono: the driver writes 0, but the driver
                               is not authoritative -- the silicon is
  0x0F / 0x10 VOL_CTRL_LO/HI   per-channel digital volume (0.5 dB steps)

Read these BEFORE touching firmware or wiring: a mono bit set by some earlier
boot, or an input select pointing both channels at one slot, would produce
exactly what the microphone measured.
"""
import subprocess
import sys
import os

# Resolve the app root from this file, not an absolute path: the tree
# lives inside the firmware repo (csd-js-tester/rig/ sits next to
# csd_diag.py) and the old scratch copy must not be baked in.
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REGS = {
    0x01: "INPUT_SELECT",
    0x02: "MIXING_N_AUTOMUTE_CONFIG",
    0x03: "ANALOG_VOL_CTRL",
    0x07: "FILTER_SHAPE_N_SYSTEM_MUTE",
    0x0A: "MASTER_MODE_N_SYNC_CONFIG",
    0x0D: "THD_BYPASS_N_MONO_MODE",
    0x0E: "SOFT_START_CONFIG",
    0x0F: "VOL_CTRL_LO",
    0x10: "VOL_CTRL_HI",
    0x40: "CHIP_STATUS",
    0x48: "INPUT_SEL_N_AUTOMUTE_STAT",
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
print("codec configuration as the silicon holds it:\n")
for a, v in zip(addrs, vals):
    print(f"  0x{a:02X} {REGS[a]:28s} = 0x{v:02X}  ({v:08b})")

print("\n=== decode the channel-relevant bits ===")
# Conditional text is built into plain variables first: an f-string cannot
# contain a quote-delimited conditional that spans lines, and it failed twice
# in this project as "unterminated string literal".
d = dict(zip(addrs, vals))
mono = (d.get(0x0D, 0) >> 4) & 1
mono_note = ("*** MONO: both channels play one channel. That explains both "
             "capsules hearing both stimuli, and makes per-channel volume "
             "meaningless" if mono else "(stereo, as the driver intends)")
sysmute = d.get(0x07, 0) & 1
mute_note = ("*** MUTED -- no analog output regardless of everything else"
             if sysmute else "unmuted")
vol_note = ("*** different: channels are configured asymmetrically"
            if d.get(0x0F) != d.get(0x10) else "equal")
print(f"  MONO_MODE (0x0D bit 4)      = {mono}  {mono_note}")
print(f"  SYSTEM_MUTE (0x07 bit 0)    = {sysmute}  {mute_note}")
print(f"  INPUT_SELECT (0x01)         = 0x{d.get(0x01, 0):02X} "
      f"({d.get(0x01, 0):08b})  -- if both channel fields select the same "
      f"source, that IS the swap/collapse")
print(f"  MIXING (0x02)               = 0x{d.get(0x02, 0):02X} "
      f"({d.get(0x02, 0):08b})  -- mixing sums L into R; would also explain "
      f"automute never asserting")
print(f"  VOL L (0x0F)=0x{d.get(0x0F, 0):02X}  VOL R (0x10)=0x{d.get(0x10, 0):02X}  {vol_note}")
print(f"  ANALOG_VOL_CTRL (0x03)      = 0x{d.get(0x03, 0):02X} "
      f"({d.get(0x03, 0):08b})")
a48 = d.get(0x48, 0)
print(f"  AUTOMUTE_STAT (0x48)        = 0x{a48:02X}   "
      f"OC_R={(a48 >> 7) & 1} OC_L={(a48 >> 6) & 1} "
      f"AM_R={(a48 >> 5) & 1} AM_L={(a48 >> 4) & 1}")
a40 = d.get(0x40, 0)
print(f"  CHIP_STATUS (0x40)          = 0x{a40:02X}   chip_id={a40 >> 2} lock={a40 & 1}")
print("\n  An overcurrent flag on one channel means the codec is shutting that")
print("  output down, which would silence one capsule while the other still")
print("  plays -- the opposite of what the microphone measured, so check it.")
