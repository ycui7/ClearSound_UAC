#!/usr/bin/env python3
"""Is the mixer volume control actually broken, or did I misread it?

Claim under test, made earlier and NOT adequately proven: "the host-visible
mixer volume is advertised but inert -- cset returns rc=0 and nothing
reaches the mixer." The evidence I had: after `amixer cset numid=6
values=96,96` the mixer's own counter showed multiplier 2137320 =
2^29*10^(-48/20), i.e. -48 dB, and cmds then froze across cset 192/48/0.

That reading has an alternative explanation I should have checked first:
updateVol() computes longMul(master_vol, vol, 29) -- master TIMES channel.
Two -24 dB stages multiply to -48 dB, so -48 dB may be arithmetically
CORRECT for that write, not a clamp to MIN_VOLUME. And "cmds froze" is what
snd-usb-audio does by design: it caches the control value and sends no USB
traffic when the value has not changed. If the kernel believed every write
resolved to the same value, silence on the bus is normal behaviour, not a
firmware fault.

Distinguish them with the one instrument this board has proven reliable --
the mixer's SET_MIX_OUT_VOL counter over SCPI:

  monotonic writes (96,192,48,0)   : reproduce the freeze. If cmds does not
                                     advance, the kernel is caching.
  ALTERNATING writes (96,0,96,0..)  : defeats change-detection. If cmds
                                     advances on every flip, SET_CUR really
                                     does go out whenever the value differs,
                                     and the control is functional at the USB
                                     level -- my 'inert' claim was wrong.
  numid=7 (the third channel/master): separate kcontrol, same path. Tests
                                     whether one control works while another
                                     does not.

For each write, report the multiplier's IMPLIED dB,
20*log10(mult/2^29), and the dB the kernel SHOULD have sent for that count
given the descriptor's -48..0 dB over 0..192. Whether implied == expected,
== expected+master, or pinned at -48 regardless is what actually settles
where the fault is, and it is printed rather than asserted.
"""
import math
import os
import re
import subprocess
import sys
import time

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
VOL_MAX = 192
DB_FLOOR = -48.0            # descriptor: MIN_VOLUME 0xD000 @ VOLUME_RES 0x40
MULT_UNITY = float(1 << 29)


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def nums(s):
    return [int(x) for x in re.findall(r"-?\d+", s)]


def card_number(*pats):
    """Card NUMBER; the pattern and the number must be on the SAME line."""
    for line in open("/proc/asound/cards"):
        m = re.search(r"^\s*(\d+)\s*\[", line)
        if not m:
            continue
        low = line.lower()
        if any(p in low for p in pats):
            return m.group(1)
    return None


def amixer(*args):
    return subprocess.run(["amixer", "-c", XMOS] + list(args),
                          capture_output=True, text=True)


def count_values(numid):
    out = amixer("cget", f"numid={numid}").stdout
    for line in out.splitlines():
        if line.strip().startswith(":"):
            m = re.search(r"values=([\d,\-]+)", line)
            if m:
                return [int(x) for x in m.group(1).split(",")]
    return None


XMOS = os.environ.get("CSD_XMOS_CARD") or card_number("xmos")
if not XMOS:
    sys.exit("no XMOS card")


def vol_state():
    v = nums(scpi("DEBUG:GENerator:VOLume?"))
    return (v + [0, 0, 0])[:3]


def implied_db(mult):
    if mult <= 0:
        return float("-inf")
    return 20 * math.log10(mult / MULT_UNITY)


def want_db(counts):
    return DB_FLOOR + (DB_FLOOR * -1) * (counts / float(VOL_MAX))


print("== 0. baseline ==")
print("  numid=6:", count_values(6), " numid=7:", count_values(7))
base = vol_state()
print(f"  GENerator:VOLume? = cmds={base[0]} idx={base[1]} mult={base[2]}"
      f" (implied {implied_db(base[2]):.2f} dB)")

print("\n== 1. monotonic writes: reproduce the reported freeze ==")
prev = base[0]
mono_advance = 0
for v in (96, 192, 48, 0):
    amixer("cset", "numid=6", f"values={v},{v}")
    time.sleep(0.5)
    c, idx, m = vol_state()
    grew = c > prev
    mono_advance += 1 if grew else 0
    print(f"  cset {v:>3}: cmds {prev} -> {c}{'  +' if grew else '  ='}  "
          f"idx={idx} mult={m}"
          f" (implied {implied_db(m):.2f} dB, want {want_db(v):.2f})")
    prev = c

print("\n== 2. ALTERNATING writes: defeat the kernel's change detection ==")
flips_advance = 0
flips = 0
prev = vol_state()[0]
for v in (96, 0, 176, 32, 192, 8, 128, 64):
    amixer("cset", "numid=6", f"values={v},{v}")
    time.sleep(0.45)
    c, idx, m = vol_state()
    grew = c > prev
    flips += 1
    flips_advance += 1 if grew else 0
    print(f"  cset {v:>3}: cmds {prev} -> {c}{'  +' if grew else '  ='}  "
          f"mult={m} (implied {implied_db(m):.2f} dB, want {want_db(v):.2f})")
    prev = c

print("\n== 3. the other playback control (numid=7) ==")
prev = vol_state()[0]
alt7_advance = 0
for v in (96, 0, 192, 48):
    amixer("cset", "numid=7", f"values={v}")
    time.sleep(0.45)
    c, idx, m = vol_state()
    grew = c > prev
    alt7_advance += 1 if grew else 0
    print(f"  cset {v:>3}: cmds {prev} -> {c}{'  +' if grew else '  ='}  "
          f"mult={m} (implied {implied_db(m):.2f} dB)")
    prev = c

print("\n== verdict ==")
print(f"  monotonic 96,192,48,0 -> {mono_advance}/4 advanced")
print(f"  alternating 8 flips   -> {flips_advance}/{flips} advanced")
print(f"  numid=7 4 flips       -> {alt7_advance}/4 advanced")
if flips_advance >= flips - 1:
    print("  => SET_CUR DOES reach the mixer whenever the value changes. The")
    print("     earlier 'frozen' reading was snd-usb-audio's change-detection")
    print("     cache, which is normal -- the control is NOT inert, and the")
    print("     -48 dB seen earlier is consistent with updateVol multiplying")
    print("     master x channel. Compare implied vs want dB above to say")
    print("     whether the level itself is right.")
elif alt7_advance >= 3:
    print("  => numid=7 works while numid=6 does not: two kcontrols, only one")
    print("     reaching the mixer. Fault is in the control->CN mapping.")
else:
    print("  => neither control produces SET_CUR traffic on value change;")
    print("     the control really is inert. Next suspect: endpoint0 SET_CUR")
    print("     handling for this unitID.")

# leave the board at full level
amixer("cset", "numid=6", "values=192,192")
amixer("cset", "numid=7", "values=192")
