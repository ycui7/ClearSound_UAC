#!/usr/bin/env python3
"""Read the unit identity over SCPI and verify it independently.

Answers three questions, in the order that makes each answer falsifiable:

  1. Is the tile[0] audio-hw task ALIVE?  csd_identity_run() is called at the
     top of AudioHwRemoteTile0, before its select loop. If an OTP read hung,
     that task never serves REGister:READ? again -- so a working register read
     is the evidence that the identity read RETURNED, and a blocked one is the
     evidence it did not. Distinguishing these two is the whole point: "no
     serial" has two very different causes and only one of them is a hang.
  2. What does the device claim?  serial, provenance, hash.
  3. Is the device's arithmetic right?  RAW? hands back the material words,
     so the FNV-1a/64 is recomputed HERE and compared. A hash nobody
     recomputes is a decoration.

Read-only: no register writes, no OTP programming (there is no OTP write path
in the firmware at all).
"""
import base64
import os
import re
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FNV64_OFFSET = 0xcbf29ce484222325
FNV64_PRIME = 0x100000001b3
MASK64 = (1 << 64) - 1


def scpi(cmd, timeout=8):
    """One command, reply text. Quoting stays inside argv -- no shell layer,
    which is what mangled the base64 payload in the ad-hoc version."""
    r = subprocess.run(
        [f"{APP}/.venv/bin/python", "csd_diag.py", "--backend", "rawmidi",
         "--timeout", str(timeout), cmd],
        capture_output=True, text=True, cwd=APP)
    out = (r.stdout or "").strip()
    if r.returncode and not out:
        return f"ERROR {(r.stderr or '').strip()[:120]}"
    return out.splitlines()[-1].strip() if out else "TIMEOUT"


def fnv1a64_be(words):
    h = FNV64_OFFSET
    for w in words:
        for b in [(w >> 24) & 0xFF, (w >> 16) & 0xFF, (w >> 8) & 0xFF, w & 0xFF]:
            h = ((h ^ b) * FNV64_PRIME) & MASK64
    return h


def main():
    rc = 0

    # -- 1. liveness of the task that performed the read -------------------
    print("=== is the tile[0] audio-hw task alive? ===")
    # Payload computed, never hardcoded: an earlier revision of this script
    # carried a literal base64 for 0x40 that actually decoded to two bytes
    # (0x40, 0x00), silently asking for a different register.
    chip_addr = 0x40                                   # CHIP_STATUS
    chip_payload = base64.b64encode(bytes([chip_addr])).decode()
    chip = scpi(f'REGister:READ? "{chip_payload}"')
    print(f"  REGister:READ? 0x{chip_addr:02X} -> {chip}")
    task_alive = False
    try:
        v = base64.b64decode(chip.strip('"'), validate=True)
        task_alive = len(v) == 1
        print(f"  chip status = 0x{v[0]:02X}  -> tile[0] task "
              f"{'ALIVE (served the read)' if task_alive else '???'}")
    except Exception as e:
        print(f"  could not decode ({e}); register path did not answer")
    print("  csd_identity_run() runs BEFORE that task's select loop, so a live")
    print("  register read means the OTP reads returned; a dead one means they hung.\n")

    # -- 2. what the device claims -----------------------------------------
    print("=== identity as the device reports it ===")
    serial = scpi('DIAG:IDENTity:SERial?')
    source = scpi('DIAG:IDENTity:SOURce?')
    hashr = scpi('DIAG:IDENTity:HASH?')
    stage = scpi('DIAG:IDENTity:STAGe?')
    raw = scpi('DIAG:IDENTity:RAW?')
    idn = scpi('*IDN?')
    print(f"  SERial? {serial}")
    print(f"  SOURce? {source}")
    print(f"  HASH?   {hashr}")
    print(f"  RAW?    {raw}")
    # 0 not started, 1 entered, 2..5 one per id read, 6 complete.
    print(f"  STAGe?  {stage}   (0 not started, 1 entered, 2..5 one per id "
          f"read, 6 complete)")
    print(f"  *IDN?   {idn}")

    # -- 3. independent recompute ------------------------------------------
    print("\n=== verify the device's hash independently ===")
    try:
        payload = raw.strip().strip('"')
        blob = base64.b64decode(payload, validate=True)
        if len(blob) % 4:
            raise ValueError(f"material is {len(blob)} bytes, not a multiple of 4")
        words = [int.from_bytes(blob[i:i + 4], "big") for i in range(0, len(blob), 4)]
    except Exception as e:
        print(f"  CANNOT VERIFY: RAW? unusable ({e})")
        return 1

    labels = ["tile0 node id", "tile1 node id", "tile0 jtag id", "tile1 jtag id",
              "otp header word", "otp serial word", "otp board id word"]
    print("  material (big-endian words, the documented order):")
    for i, w in enumerate(words):
        lab = labels[i] if i < len(labels) else "?"
        print(f"    [{i}] {lab:18s} 0x{w:08X}")

    expect = fnv1a64_be(words)
    try:
        got = int(hashr.strip().strip('"'), 16)
    except Exception:
        print(f"  HASH? not hex: {hashr!r}")
        return 1
    ok = expect == got
    print(f"  host FNV-1a/64 = 0x{expect:016X}")
    print(f"  device says    = 0x{got:016X}   {'MATCH' if ok else 'MISMATCH'}")
    if not ok:
        rc = 1

    # The serial must equal the tail of the hash the HOST recomputed, not the
    # tail of the hash the device claims. Checking it against the device's own
    # HASH? reply would only prove the device is self-consistent -- a device
    # with a broken hash would still agree with itself.
    s = serial.strip().strip('"')
    want_serial = "%08x" % (expect & 0xFFFFFFFF)
    if not re.match(r"^[0-9a-f]{8}$", s):
        print(f"  FAIL  serial {s!r} is not exactly 8 lowercase hex characters")
        rc = 1
    elif s != want_serial:
        print(f"  FAIL  serial {s} != tail of recomputed hash ({want_serial})")
        rc = 1
    else:
        print(f"  serial {s} == low 32 bits of the recomputed hash  OK")

    # -- verdict ------------------------------------------------------------
    print("\n=== verdict ===")
    all_zero = all(w == 0 for w in words)
    if all_zero:
        print("  OTP reads all zero: this board is UNPROVISIONED. Expected on a")
        print("  bench board. The serial must therefore be DERIVED, never OTP:")
        if "DERIVED" in source:
            print("  PASS  provenance correctly reports DERIVED for a blank OTP")
        else:
            print(f"  FAIL  blank OTP but provenance says {source} -- the device")
            print("        is claiming an authority it does not have")
            rc = 1
    else:
        print(f"  OTP carries data: header=0x{words[4]:08X} serial=0x{words[5]:08X}")
        want = "OTP" if words[5] else "DERIVED"
        print(f"  {'PASS' if want in source else 'FAIL'}  provenance {source} (want {want})")
        if want not in source:
            rc = 1

    if not task_alive:
        print("  FAIL  tile[0] register path did not answer -- the identity read")
        print("        likely hung, which would take the codec server with it")
        rc = 1

    # Stage is the tie-breaker when material is all zero: blank OTP (3) and a
    # stalled read (1) look identical in RAW? and demand opposite responses.
    try:
        stage_n = int(stage.strip().strip('"'))
    except Exception:
        stage_n = -1
        print(f"  FAIL  STAGe? unreadable ({stage})")
        rc = 1
    # The stage is the whole point of the staged design: on a virgin board every
    # material word is legitimately zero, so RAW? alone cannot tell "OTP is
    # blank" (stage 6) from "the read stalled at the first access" (stage 1).
    # Those two demand opposite responses and would otherwise be identical here.
    if stage_n != 6:
        where = {0: "thread never started",
                 1: "stalled before the first read",
                 2: "stalled in tile0 node id",
                 3: "stalled in tile0 JTAG id",
                 4: "stalled in tile1 node id (CROSS-TILE read)",
                 5: "stalled in tile1 JTAG id (CROSS-TILE read)"}.get(
                     stage_n, f"unknown stage {stage_n}")
        print(f"  FAIL  staged read incomplete: stage {stage_n} -- {where}")
        rc = 1

    # UNAVAILABLE means the tile-1 core could not reach the tile-0 owner at all
    # -- a transport fault, which is a different bug from "the read stalled" and
    # must not be reported as one.
    if serial.strip('"') == "UNAVAILABLE":
        print("  FAIL  the tile-1 core could not fetch from the tile-0 owner")
        print("        (transport broken, not the identity read)")
        rc = 1

    if serial.strip('"') in ("IPENDING", "TIMEOUT", "ERROR"):
        print(f"  FAIL  serial never published ({serial})")
        rc = 1

    print(f"\n{'IDENTITY: PASS' if rc == 0 else 'IDENTITY: FAIL'}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
