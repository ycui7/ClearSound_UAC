#!/usr/bin/env python3
"""Leave the board at its documented power-up state: L/R trim 0 dB / 0 dB.

MUST be run through scratch/run_xe.sh, i.e. inside ONE xrun life. Sending
these commands after run_xe.sh returns does nothing: it kills xrun at the end
of its pass, the board drops back off our build, and VOLume:TRIM:* then hits a
firmware that has no such command -- observed as
  TIMEOUT: no reply and no queued error for 'VOLume:TRIM?'
which is exactly what an out-of-life attempt looked like. Same trap as the
backgrounded-xrun note in LOG.md.

Why this exists at all: rig/trim_persist_check.py ends the verify pass by
putting the ARMED pair back (it must, or its own acoustic legs would not
repeat). That leaves a shipped board boots attenuated by 8 and 16 dB. This
script is the "leave it clean" step, and it asserts rather than trusts.
"""
import os
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(APP, ".venv", "bin", "python")


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return ((rr.stdout or "") + (rr.stderr or "")).strip()


def ints(out):
    """Digit runs only: SYST:ERR? answers 0,"No error" and int('"No') raises.
    (The bug that killed the first rig run on hardware.)"""
    import re
    return [int(t) for t in re.findall(r"-?\d+", out)]


print("BEFORE status:", scpi("VOLume:TRIM:STATus?"))

scpi("VOLume:TRIM:LEFT 0")
scpi("VOLume:TRIM:RIGHT 0")

rb = ints(scpi("VOLume:TRIM?"))
st = ints(scpi("VOLume:TRIM:STATus?"))
print("READBACK:", rb)
print("AFTER  status:", st)

rc = 0
if rb != [0, 0]:
    print("BAD read-back is not 0,0 -- the board would boot attenuated")
    rc = 1
if len(st) < 7:
    print("BAD no status reply: is the persistence build loaded? "
          "(partition,load,save,seq,err,boots,saves)")
    rc = 1
else:
    part, load, save, seq, err, boots, saves = st[:7]
    if part == 0:
        print("BAD data partition absent: board flashed without --data")
        rc = 1
    if err != 0:
        print(f"BAD flash error latched: {err:#x}")
        rc = 1
    # Zeroing must JOURNAL, otherwise the next boot re-adopts the armed pair
    # from flash and the board is attenuated again -- the read-back above would
    # still read 0,0 and lie. So check the counters moved, not just the value.
    if save != 1:
        print(f"BAD the 0 dB pair was not journalled (save={save})")
        rc = 1
    elif seq == 0:
        print("BAD seq==0 with save==1: nothing is in the journal")
        rc = 1
    else:
        print(f"OK 0 dB/0 dB journalled (seq={seq}, saves={saves}) -- a boot "
              f"now adopts 0 dB, which is the documented reset default")

# SYST:ERR? must be empty: a rejected set would otherwise pass the read-back
# check while the write never happened. 0 means empty, NOT truthy evidence.
e = ints(scpi("SYST:ERR?"))
if e and e[0] != 0:
    print(f"BAD error queue reports {e}")
    rc = 1

print("ZERO-OK" if rc == 0 else "ZERO-FAILED")
sys.exit(rc)
