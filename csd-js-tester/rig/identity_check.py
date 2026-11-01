#!/usr/bin/env python3
"""Read the unit identity over SCPI and verify it independently.

Answers three questions, in the order that makes each answer falsifiable:

  1. Is the tile[0] audio-hw task ALIVE?  csd_identity_run() is called at the
     top of AudioHwRemoteTile0, before its select loop. If the flash serial
     read (Winbond 4Bh) hung,
     that task never serves REGister:READ? again -- so a working register read
     is the evidence that the identity read RETURNED, and a blocked one is the
     evidence it did not. Distinguishing these two is the whole point: "no
     serial" has two very different causes and only one of them is a hang.
  2. What does the device claim?  serial, provenance, hash.
  3. Is the device's arithmetic right?  RAW? hands back the material words,
     so the FNV-1a/64 is recomputed HERE and compared. A hash nobody
     recomputes is a decoration.

Read-only: no register writes, no flash writes (the identity read is 4Bh;
there is no OTP path at all since the OTP read was retired 2026-11-01) (
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
    print("  register read means the server reached its select loop; a dead")
    print("  one means the identity flash read ahead of it never returned.\n")

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
              "flash serial hi", "flash serial lo"]
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
    # Material [4],[5] are the QSPI die's factory 64-bit serial -- the only
    # per-unit input the device has. Words 0..3 are board-configuration
    # constants (tile node ids + JTAG IDCODE) and are identical on every board
    # of this design, which is exactly why the flash serial replaced the OTP
    # read: while they were the only material all three bench boards hashed to
    # the SAME serial.
    ser = (words[4] << 32) | words[5] if len(words) > 5 else 0
    if ser == 0:
        # The datasheet has no blank-serial case, and the firmware refuses to
        # hash an all-00/all-FF answer, so zero means the READ failed.
        print("  FAIL  flash unique serial is ZERO -- the read did not land.")
        print("        The serial on offer is DERIVED, i.e. a hash of four")
        print("        board-configuration constants: NOT unique. Do not book")
        print("        this board's identity against it.")
        if "DERIVED" not in source:
            print(f"        WORSE: provenance says {source}, claiming an")
            print("        authority the data does not support.")
        rc = 1
    elif ser == 0xFFFFFFFFFFFFFFFF:
        print("  FAIL  flash serial is all-FF (DO floating) -- not a read.")
        rc = 1
    else:
        print(f"  flash unique serial = 0x{ser:016X}")
        if "FLASH" in source:
            print("  PASS  provenance FLASH: per-unit identity from the die")
        else:
            print(f"  FAIL  serial is non-zero but provenance says {source}")
            rc = 1

    if not task_alive:
        print("  FAIL  tile[0] register path did not answer -- the identity read")
        print("        likely hung, which would take the codec server with it")
        rc = 1

    # Stage is the tie-breaker when the serial is zero: "the server never got
    # there" (0) and "the flash read was attempted and failed" (>=1) look the
    # same in RAW? and demand opposite responses. The old 3..6 OTP-walk
    # stages are retired with the OTP path.
    try:
        stage_n = int(stage.strip().strip('"'))
    except Exception:
        stage_n = -1
        print(f"  FAIL  STAGe? unreadable ({stage})")
        rc = 1
    if stage_n != 2:
        where = {0: "codec server never reached the identity block",
                 1: "stalled in the flash read (fl_connect entered, no answer)"
                 }.get(stage_n, f"unknown stage {stage_n}")
        print(f"  FAIL  identity read incomplete: stage {stage_n} -- {where}")
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
