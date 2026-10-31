#!/usr/bin/env python3
"""Read the codec's own signal-source configuration, over the proven quoted
REGister:READ? path. All modules behind the XU316's last digital word are
clean (mirror tap + post-DSP USB-IN tap, both tone-validated), so the only
firmware-side candidates left that can SOUND on silent data are the chip's
internal generators and route select -- which our boot writes NOWHERE:

  0x01 INPUT_SELECT      source select per the datasheet (I2S vs internal)
  0x22-0x25 PROGRAMMABLE NCO  a tone generator on this chip. If enabled
                         (nonzero num) it plays REGARDLESS of I2S data:
                         exactly "zeros in, tone out" at a fixed frequency.
  0x07 FILTER/MUTE       is the chip even unmuted normally
  0x0D MONO              a stuck mono bit duplicates one channel
  0x02/0x04/0x05 automute block (expected reset: 0x02 default, 0x04=0 off)
  0x48/0x40 status       decode the per-channel automute bits

Reads only. The proven call form is  REGister:READ? "b64"  -- QUOTED
(identity_check.py, codec_config_read.py); an unquoted token made every
read return nothing, which is how the automute verification failed once.
"""
import base64
import re
import subprocess
import sys

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"

READS = [
    (0x01, "INPUT_SELECT"),
    (0x02, "MIXING_N_AUTOMUTE"),
    (0x03, "ANALOG_VOL"),
    (0x04, "AUTOMUTE_TIME"),
    (0x05, "AUTOMUTE_LEVEL"),
    (0x06, "DOP_N_VOL_RAMP"),
    (0x07, "FILTER_N_MUTE"),
    (0x0D, "THD_BYPASS_N_MONO"),
    (0x0F, "VOL_CH1"),
    (0x10, "VOL_CH2"),
    (0x22, "NCO_NUM_22"),
    (0x23, "NCO_NUM_23"),
    (0x24, "NCO_NUM_24"),
    (0x25, "NCO_NUM_25"),
    (0x40, "CHIP_STATUS"),
    (0x48, "AUTOMUTE_STAT"),
]


def scpi(cmd, timeout=10):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def read(addrs):
    tok = base64.b64encode(bytes(addrs)).decode()
    out = scpi(f'REGister:READ? "{tok}"')
    out = out.strip().strip('"')
    try:
        return list(base64.b64decode(out, validate=True))
    except Exception:
        return None


print(scpi("*IDN?"))
vals = read([a for a, _ in READS])
if vals is None or len(vals) != len(READS):
    sys.exit(f"readback failed: {vals}")

suspects = []
for (addr, name), v in zip(READS, vals):
    print(f"  0x{addr:02X} {name:20s} = 0x{v:02X} ({v:3d})")
    if 0x22 <= addr <= 0x25 and v != 0:
        suspects.append(f"NCO reg 0x{addr:02X} nonzero -> tone generator live")
    if addr == 0x01 and (v & 0x03) != 0x00:
        suspects.append(f"INPUT_SELECT low bits {v & 0x03} != 0 -- check source")
    if addr == 0x07 and (v & 0x01):
        suspects.append("SYSTEM MUTE bit set while (presumably) playing")
    if addr == 0x0D and (v & 0x10):
        suspects.append("MONO_MODE set")

print("\nverdict:")
if suspects:
    for s in suspects:
        print("  SUSPECT:", s)
    sys.exit(1)
print("  codec signal-source config clean: NCO off, input select nominal,"
      " not muted, not mono. The chip generates nothing internally on zeros.")
