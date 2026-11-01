#!/usr/bin/env python3
"""Smoke the REGister:WRITE path against a harmless register BEFORE any
experiment depends on it. Writes go (addr,val) interleaved; the response is
an empty SCPI ack (rc=0, no ERROR). Target: reg 0x05 AUTOMUTE_LEVEL, whose
reset value IS 104, so writing 104 then reading 104 proves the round trip
without changing anything. Then a 0x60/restore leg proves the value
actually moves and comes back. Abort on any mismatch -- an experiment run
on a broken write path produced one void result today."""
import base64
import re
import subprocess
import sys

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"


def scpi(cmd, timeout=10):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    out = (rr.stdout or "").strip()
    if "ERROR" in out.upper():
        print(f"  scpi error on {cmd!r}: {out}")
    return out


def rd(addrs):
    tok = base64.b64encode(bytes(addrs)).decode()
    out = scpi(f'REGister:READ? "{tok}"').strip().strip('"')
    try:
        return list(base64.b64decode(out, validate=True))
    except Exception:
        return None


def wr(pairs):
    tok = base64.b64encode(bytes([b for ab in pairs for b in ab])).decode()
    out = scpi(f'REGister:WRITE "{tok}"')
    return "ERROR" not in out.upper()


print(scpi("*IDN?"))
fails = 0

v = rd([0x05])
print(f"reg5 reset read: {v}")
if v != [104]:
    print("  (reset value not 104 -- proceeding anyway, will restore)")

if not wr([(0x05, 104)]):
    print("WRITE PATH DEAD (no ack)"); sys.exit(1)
v = rd([0x05])
print(f"after write 104: {v}"); fails += (v != [104])

if not wr([(0x05, 0x60)]):
    print("WRITE PATH DEAD (no ack)"); sys.exit(1)
v = rd([0x05])
print(f"after write 96 : {v}"); fails += (v != [96])

if not wr([(0x05, 104)]):
    sys.exit(1)
v = rd([0x05])
print(f"restore 104    : {v}"); fails += (v != [104])

print("WRITE/READ PATH:", "OK" if fails == 0 else f"FAIL ({fails})")
sys.exit(1 if fails else 0)
