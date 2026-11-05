#!/usr/bin/env python3
"""Why is the DAC silent? Print every state that decides output level at once.

Case 09 went from -21.9 dBFS to -98.7 dBFS after a gate run. The candidates live
in different places -- trims and what the journal thinks they are, the codec's
analog / per-channel / master volume words, system mute, the idle-power state --
so reading them one at a time is how you convict the wrong one.

Register reads go through b64read, which also reads 0x03 and 0x07 as WITNESSES:
a codec held in reset answers I2C with zeros, so an all-zero read is either the
truth or nobody home, and the witnesses are what tell those apart.
"""
import sys

sys.path.insert(0, ".")
from rig.csd_suite import scpi, cards, b64read  # noqa: E402

dac = [c for c, n in cards().items() if n and "DACUAC20" in n][0]
print("dac card", dac)


def q(cmd):
    ok, v, eq = scpi(cmd, dac, timeout=10)
    print("  %-26s %s" % (cmd, (v.strip() if ok else "ERR " + str(eq))))


print("-- path --")
q("*IDN?")
q("DIAG:DSP:PRESet?")
q("DEBUG:LOOPback?")
print("-- trim / journal / power --")
q("VOLume:TRIM?")
q("VOLume:TRIM:STATus?")
q("POWER:TIMEout?")
print("-- codec registers --")
# 1 = system mute/control, 3 = analog volume (witness, expect 40), 7 = expect 00,
# 15/16 = per-channel digital volume, 17..20 = master trim (0dB = FF FF FF 7F),
# 27 = general config (LR volume sharing lives near here), 0x44/0x45 = ADC side.
vals, err = b64read([0x01, 0x0F, 0x10, 0x11, 0x12, 0x13, 0x14, 0x1B], dac)
print("  read:", "ERR " + str(err) if err else
      " ".join("%02X" % b for b in vals),
      "(order: 01 sysmute, 0F ch1, 10 ch2, 11-14 master, 1B gencfg)")
if vals:
    m = (vals[3] << 24) | (vals[4] << 16) | (vals[5] << 8) | vals[6]
    print("  master trim word = 0x%08X  (%s)" % (
        m & 0xFFFFFFFF, "unity 0 dB" if m == 0x7FFFFFFF else "ATTENUATED"))
print("-- host side --")
import subprocess  # noqa: E402
for n in (5, 6):
    r = subprocess.run(["amixer", "-c", str(dac), "cget", "numid=%d" % n],
                       capture_output=True, text=True)
    line = [l for l in r.stdout.splitlines() if "values=" in l]
    print("  numid %-24d %s" % (n, line[-1].strip() if line else "?"))
