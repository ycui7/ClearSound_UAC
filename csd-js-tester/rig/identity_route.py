#!/usr/bin/env python3
"""Tell "the identity thread never ran" from "its results are invisible".

Why this exists as a separate script rather than more logging inside the
firmware: identity_check.py reported STAGe?=0 forever, even after the stage
counter was set as the task's FIRST statement. Two causes fit and they need
opposite fixes:

  (a) the thread is never scheduled -- nothing to fix in the read code;
  (b) the thread runs, but the SCPI core reads a different copy of the
      statics (they live in .dp.bss, i.e. DP/CP-relative), so the numbers
      exist and only the query path is broken.

Adding firmware prints cannot settle it: print* rides the xscope link and can
block, which is a hazard this project already paid for on an audio thread, and
in either case it reads the same statics through the same suspect path.

So use a consumer that is NOT the SCPI path. csd_identity_task() itself calls
XUA_Endpoint0_setSerialStr(s_str), copying the serial into lib_xua's string
table; the host then reads iSerialNumber out of the USB descriptor. That path
never touches my SCPI handlers.

  iSerial == "123456789"  -> the copy never happened -> cause (a)
  iSerial == "DV..."      -> thread ran and computed  -> cause (b)

Read-only: no register writes, no OTP programming.
"""
import os
import re
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return (r.stdout or "").strip()


def scpi(cmd, timeout=8):
    r = subprocess.run(
        [f"{APP}/.venv/bin/python", "csd_diag.py", "--backend", "rawmidi",
         "--timeout", str(timeout), cmd],
        capture_output=True, text=True, cwd=APP)
    out = (r.stdout or "").strip()
    return out.splitlines()[-1].strip() if out else "TIMEOUT"


print("=== USB descriptor serial (read by the host, bypasses the SCPI path) ===")
desc = sh("lsusb -d 20b1:0018 -v 2>/dev/null | grep -m3 'iSerial\\|iProduct'")
print(desc or "  (no descriptor)")

# The serial is now 8 lowercase hex characters: the low 32 bits of the identity
# hash. Two legacy values must NOT be mistaken for it.
#   123456789  SERIAL_STR, the compile-time constant in the factory image ->
#              setSerialStr never ran, so the identity thread never executed.
#   IPENDING   our marker, pushed at thread entry before any hardware access ->
#              the thread started and then stalled in a read.
# Both are unambiguous because a real serial is exactly 8 lowercase hex chars
# and neither of them matches that shape.
m = re.search(r'iSerial\s+\d+\s+(\S+)', desc)
got = m.group(1) if m else ""
if got == "123456789":
    print("\n  VERDICT: iSerial is still the factory SERIAL_STR.")
    print("  -> the identity thread never executed. Not OTP, not sswitch.")
elif got == "IPENDING":
    print("\n  VERDICT: the thread started (entry publish landed) but never")
    print("  -> finished the reads. Compare with STAGe? below: the stage names")
    print("     the access it stalled on.")
elif re.match(r"^[0-9a-f]{8}$", got):
    print(f"\n  VERDICT: the thread completed and published serial {got}.")
    print("  -> iSerial is authoritative; it is written on tile[0], the tile")
    print("     that owns g_strTable, so it needs none of the cross-tile")
    print("     forwarding the SCPI queries depend on.")
else:
    print("\n  VERDICT: inconclusive -- iSerial is neither a legacy value nor")
    print("  8 lowercase hex. Do not guess; capture the descriptor and look.")

print("\n=== same data through the SCPI path, for contrast ===")
print(f"  STAGe?         {scpi('DIAG:IDENTity:STAGe?')}")
print(f"  SERial?        {scpi('DIAG:IDENTity:SERial?')}")
print(f"  SOURce?        {scpi('DIAG:IDENTity:SOURce?')}")
print("\nThe two serials must agree. If they do not, the tile-0 -> tile-1 fetch")
print("and the tile-0 descriptor push are reading different state.")
