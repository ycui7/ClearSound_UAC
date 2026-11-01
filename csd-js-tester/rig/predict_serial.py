#!/usr/bin/env python3
"""Independent host-side prediction of the CSD serial from the flash die ID.

Reimplements the firmware's fnv1a64_be over the six material words so the
rig's own "MATCH" is cross-checked by something that does not share the
firmware's code. If firmware and this ever disagree, one of them is wrong --
a rig that recomputes with the same algorithm as the device proves wiring,
not correctness, so the constants here are written out longhand.
"""
import sys

MASK64 = (1 << 64) - 1
FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3


def fnv1a64_be(words):
    """FNV-1a/64 over each 32-bit word big-endian, MSB first -- the order the
    wire uses, so a host that replays the bytes gets the device's number."""
    h = FNV_OFFSET
    for w in words:
        for shift in (24, 16, 8, 0):
            h ^= (w >> shift) & 0xFF
            h = (h * FNV_PRIME) & MASK64
    return h


def serial_from(words):
    h = fnv1a64_be(words)
    return h, format(h & 0xFFFFFFFF, "08x")


FLASH_GOOD = 0x443258C251377B1C      # datasheet value, standalone probe + div3
FLASH_SHIFT = 0x22192C61289BBD8E     # what the un-pinned read returned

if __name__ == "__main__":
    # sswitch words are board configuration constants, identical on all three
    # bench boards; pass them in on the command line when known.
    if len(sys.argv) >= 5:
        sw = [int(a, 0) for a in sys.argv[1:5]]
    else:
        print("usage: predict_serial.py <node0> <node1> <jtag0> <jtag1>")
        print("Read them from rig/identity_check.py's material dump.")
        sys.exit(2)

    for name, uid in (("GOOD (div 3, datasheet)", FLASH_GOOD),
                      ("SHIFTED (phase 1)", FLASH_SHIFT)):
        words = sw + [(uid >> 32) & 0xFFFFFFFF, uid & 0xFFFFFFFF]
        h, ser = serial_from(words)
        print(f"  {name:26s} uid=0x{uid:016X}")
        print(f"    hash   = 0x{h:016X}")
        print(f"    serial = {ser}")
        print()
