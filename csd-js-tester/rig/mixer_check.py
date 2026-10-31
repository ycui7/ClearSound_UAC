#!/usr/bin/env python3
"""What does the HOST see when the mixer is enabled?

A passing build proves the mixer core fits on a tile. It does not prove the
device EXPOSES a controllable mixer, which is the part a host or DAW actually
uses -- the descriptors are gated separately, on XUA_MIXER_EN &&
XUA_MAX_MIX_COUNT > 0. So this reads the USB configuration descriptor and
reports the AudioStreaming units.

Parse format note: lsusb -v decodes the subtype itself as
    bDescriptorSubtype      6 (FEATURE_UNIT)
decimal number plus the name in parentheses. It does NOT print "CS_FEATURE_UNIT";
an earlier version of this script matched on that and therefore found zero
descriptors of any kind, reporting a confident FAIL against a device that
plainly had FEATURE_UNITs. Count on the parenthesised name, and sanity-check
that FEATURE_UNIT and the terminals are non-zero before believing any zero.
"""
import re
import subprocess
import sys

VIDPID = "20b1:0018"

# Terms we expect in any working UAC2 streaming interface, used as a parser
# self-test: if these are zero, the parse is broken, not the firmware.
SANITY = ("INPUT_TERMINAL", "OUTPUT_TERMINAL", "FEATURE_UNIT")


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


desc = sh(f"lsusb -d {VIDPID} -v 2>/dev/null")
if not desc.strip():
    print(f"FAIL: device {VIDPID} not present")
    sys.exit(1)

# All decoded subtype names lsusb printed, e.g. "... (MIXER_UNIT)"
names = re.findall(r"bDescriptorSubtype\s+\d+\s+\(([A-Z_ ]+)\)", desc)
counts = {}
for n in names:
    counts[n.strip()] = counts.get(n.strip(), 0) + 1

print("=== audio class descriptors seen by the host ===")
if not counts:
    print("  FAIL: parser found no class subtypes at all.")
    print("        That is a parser fault, not a device fault -- check the lsusb")
    print("        output format before concluding anything about the firmware.")
    sys.exit(1)
for k, v in sorted(counts.items()):
    print(f"  {k:22s} x{v}")

# Parser self-test. A zero here invalidates every other number below.
missing = [s for s in SANITY if counts.get(s, 0) == 0]
if missing:
    print(f"\n  FAIL  parser self-test: expected {', '.join(missing)} and found none.")
    print("        Do not trust the mixer count; the parse is broken.")
    sys.exit(1)

mixers = counts.get("MIXER_UNIT", 0)
print("\n=== verdict ===")
if mixers:
    print(f"  PASS  {mixers} Mixer Unit(s) exposed to the host")
else:
    print("  FAIL  no Mixer Unit in the descriptor, though the core built.")
    print("        Descriptors are gated on XUA_MIXER_EN && XUA_MAX_MIX_COUNT > 0;")
    print("        confirm -DMIXER=1 actually reached the compiler (it travels via")
    print("        EXTRA_BUILD_FLAGS), and that this is OUR image and not factory")
    print("        -- factory has no SCPI responder, so *IDN? would have timed out.")

# Selector units are what make a mixer source switchable; their absence would
# mean fixed routing regardless of what the host requests.
sel = counts.get("SELECTOR_UNIT", 0)
print(f"  Selector Units: {sel}"
      + ("" if sel else "  (fixed routing: sources cannot be re-pointed by the host)"))

rc = 0 if mixers else 1
print(f"\n{'MIXER DESCRIPTORS: PASS' if rc == 0 else 'MIXER DESCRIPTORS: FAIL'}")
sys.exit(rc)
