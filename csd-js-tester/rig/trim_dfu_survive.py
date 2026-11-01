#!/usr/bin/env python3
"""Close TODO item 3's last leg: does the trim journal survive a DFU firmware
update?

Why this exists. TODO.md item 3 says plainly: "A plain DFU firmware update ...
writes boot-partition images only and MUST leave the trim intact -- that leg is
a rig requirement, not an assumption." It has never been executed: during the
DFU-twice protocol the journal was blank (the flash seeded it empty), so the
question "does DFU wipe persistence?" was answered by nothing at all. The
TODO's own wipe-note lists --data, --write-all and a changed
--boot-partition-size as the erasers, and excludes plain DFU -- but excludes it
by reasoning, not by measurement.

Method, and why it is shaped this way:
  MODE=arm     set LEFT=-64, RIGHT=-128 counts (-8 dB / -16 dB), confirm the
               device echoes them and that the journal advanced.
  (wrapper)    device reboot to clear the DFU state machine, then the SAME
               release .bin over DFU, then exit DFU.
  MODE=verify  a NEW boot of the DFU-written image must report the same pair.
               If DFU erased the data partition, the journal has no valid
               record and boot_load falls back to 0 dB -> the pair reads
               [0, 0]. That is the failure signature, and it is distinguishable
               from "device silent" because every query is checked individually.

All queries run on ONE open rawmidi fd. That is deliberate: csd_diag.py opens
and closes the substream per query, and a reply produced with no reader
attached is lost (observed: it answered its own load probe, then went silent
for six while the firmware parsed and F7-terminated all six). A rig that
mis-handles the fd would report a wiped journal as a dead device.

Exit codes: 0 ok, 1 the tested property failed, 3 could not start / a leg did
not complete (never a verdict).
"""
import os, re, select, sys, time

FW_SRC = ("/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac"
          "/src/extensions/csd_diag_midi.xc")
EXPECT = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester/rig/.trim_dfu_expect"
ARM_L, ARM_R = -64, -128          # counts; -8.00 dB / -16.00 dB at 0.125 dB


def sysex_id():
    """From the firmware, never hard-coded: rx_event abandons a message whose
    id byte mismatches WITHOUT replying (csd_diag_midi.xc:121-123), which is
    indistinguishable from a dead device."""
    txt = open(FW_SRC, errors="ignore").read()
    m = re.search(r"#define\s+CSD_SYSEX_ID\s+(0x[0-9A-Fa-f]+)", txt)
    if not m:
        sys.exit("CSD_SYSEX_ID not found in csd_diag_midi.xc")
    return int(m.group(1), 16)


def node_path():
    cards = open("/proc/asound/cards").read()
    # Both product names: release is 'ClearSound DAC (UAC2.0)', debug is
    # 'XMOS (UAC2.0)' (xua_defs.h:131-141).
    m = re.search(r"^\s*(\d+)\s*\[.*\]:.*(ClearSound|XMOS)", cards, re.M | re.I)
    if not m:
        sys.exit("no ClearSound/XMOS card in /proc/asound/cards")
    return f"/dev/snd/midiC{m.group(1)}D0"


def write_all(f, data):
    off, deadline = 0, time.time() + 2.0
    while off < len(data):
        n = os.write(f, data[off:])
        if n <= 0:
            if time.time() > deadline:
                raise RuntimeError(f"write stalled at {off}/{len(data)}")
            time.sleep(0.001)
            continue
        off += n


def drain_reply(f, secs=5.0):
    """Read until the F7 terminator. Stopping on a timer alone can return a
    partial reply and make a working device look wrong; stopping only on F7
    with a deadline makes a missing terminator visible as a timeout."""
    buf = b""
    t0 = time.time()
    while time.time() - t0 < secs:
        r, _, _ = select.select([f], [], [], 0.2)
        if not r:
            continue
        try:
            buf += os.read(f, 4096)
        except BlockingIOError:
            continue
        if b"\xf7" in buf:
            return buf, True
    return buf, False


def ask(f, sid, cmd, secs=5.0):
    body = bytes([0xF0, sid]) + bytes(b & 0x7F for b in (cmd + "\r\n").encode()) + b"\xF7"
    _ = drain_reply(f, 0.3)                    # clear any straggler
    write_all(f, body)
    return drain_reply(f, secs)


def nums(out):
    """Numbers out of a SysEx reply. Text replies (e.g. 'No error') contain
    words, which is why this filters instead of int()-ing every token -- an
    earlier rig crashed on '\"No'."""
    txt = out.decode("latin-1")
    return [int(t) for t in re.findall(r"-?\d+", txt)]


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "arm"
    sid = sysex_id()
    p = node_path()
    print(f"node: {p}  sysex id 0x{sid:02X}", flush=True)
    f = os.open(p, os.O_RDWR)
    incomplete = 0
    try:
        if mode == "arm":
            # A successful VOLume:TRIM:LEFT/RIGHT returns SCPI_RES_OK with NO
            # result data (csd_diag_scpi.c:1180-1208) -- it sends nothing. So
            # demanding an F7 on a set would report a WORKING board as
            # incomplete. Per that source's own comment, the way to tell
            # "saved" from "set but not journalled" is the error queue: a
            # rejected journal pushes EXECUTION_ERROR rather than being
            # swallowed. Read SYST:ERR? after each set.
            for cmd in (f"VOLume:TRIM:LEFT {ARM_L}", f"VOLume:TRIM:RIGHT {ARM_R}"):
                out, term = ask(f, sid, cmd)
                print(f"  set {cmd!r} -> {len(out)}B (no reply expected on success)")
                e, et = ask(f, sid, "SYST:ERR?")
                print(f"    SYST:ERR? -> {e[:44]!r}")
                if not et:
                    incomplete += 1          # a query with no terminator is real
                if b"Execution error" in e or b"-10" in e:
                    print("    BAD journal rejected the write (EXECUTION_ERROR)")
                    return 1
            out, term = ask(f, sid, "VOLume:TRIM?")
            print(f"  VOLume:TRIM? -> {out[:40]!r}")
            got = nums(out)
            st, _ = ask(f, sid, "VOLume:TRIM:STATus?")
            print(f"  VOLume:TRIM:STATus? -> {st[:48]!r}")
            open(EXPECT, "w").write(f"{ARM_L},{ARM_R}\n")
            # [LEFT,RIGHT] counts back as armed, journal advanced (word 3 =
            # seq, word 6 = save_n per csd_trim_store.c:464-476), no flash
            # error (word 4).
            if got != [ARM_L, ARM_R]:
                print(f"  BAD armed read-back {got} != [{ARM_L},{ARM_R}]")
                return 1
            sn = nums(st)
            if len(sn) < 7:
                print("  BAD STATus? reply too short -- cannot judge")
                return 3
            if sn[4] != 0:
                print(f"  BAD err counter = {sn[4]}")
                return 1
            if sn[6] < 2:
                print(f"  BAD save_n = {sn[6]}, expected >=2 (one per set)")
                return 1
            print(f"  OK armed [{ARM_L},{ARM_R}], seq={sn[3]}, save_n={sn[6]}, err=0")
            print("ARMED seq=%d save_n=%d" % (sn[3], sn[6]))
            return 0 if incomplete == 0 else 3

        if mode == "zero":
            # Leave the board at the documented 0 dB default. A persistence
            # test deliberately journals -8/-16 dB, and if that is left in
            # place the next acoustic gate measures an offset DAC and blames
            # the mixer. Sets produce no reply (csd_diag_scpi.c:1180-1208), so
            # the verdict comes from VOLume:TRIM? plus the error queue.
            for cmd in ("VOLume:TRIM:LEFT 0", "VOLume:TRIM:RIGHT 0"):
                ask(f, sid, cmd)
                e, et = ask(f, sid, "SYST:ERR?")
                print(f"  {cmd!r} -> SYST:ERR? {e[:44]!r}")
                if not et:
                    incomplete += 1
                if b"Execution error" in e:
                    print("    BAD journal rejected the write")
                    return 1
            out, term = ask(f, sid, "VOLume:TRIM?")
            print(f"  VOLume:TRIM? -> {out[:40]!r}")
            got = nums(out)
            if got != [0, 0]:
                print(f"  FAIL trim not zeroed: {got}")
                return 1
            st, _ = ask(f, sid, "VOLume:TRIM:STATus?")
            print(f"  VOLume:TRIM:STATus? -> {st[:48]!r}")
            print("ZERO-OK board at the documented 0 dB default")
            return 0 if incomplete == 0 else 3

        if mode == "verify":
            want = [int(x) for x in open(EXPECT).read().strip().split(",")]
            out, term = ask(f, sid, "VOLume:TRIM?")
            print(f"  VOLume:TRIM? -> {out[:40]!r}")
            got = nums(out)
            st, _ = ask(f, sid, "VOLume:TRIM:STATus?")
            print(f"  VOLume:TRIM:STATus? -> {st[:48]!r}")
            sn = nums(st)
            if got != want:
                print(f"  FAIL trim did not survive DFU: {got} != {want}")
                if got == [0, 0]:
                    print("    [0,0] is the no-valid-record fallback, i.e. the")
                    print("    journal was erased -- DFU DOES wipe persistence.")
                print("    (If every query above read 0 bytes, this is NOT a")
                print("    persistence verdict -- it is a dead/unenumerated node.)")
                return 1
            if len(sn) < 7:
                print("  INCOMPLETE STATus? -- trim values matched but the")
                print("    journal counters could not be read.")
                return 3
            print(f"  OK trim survived the DFU update: {got}")
            print(f"  journal: partition={sn[0]} load={sn[1]} save={sn[2]} "
                  f"seq={sn[3]} err={sn[4]} load_n={sn[5]} save_n={sn[6]}")
            if sn[0] == 0:
                print("  BAD partition=0 -- the data partition is gone.")
                return 1
            if sn[4] != 0:
                print(f"  BAD err counter = {sn[4]}")
                return 1
            print(f"SURVIVED {got} partition={sn[0]} seq={sn[3]} err=0")
            return 0

        sys.exit(f"unknown mode {mode!r}")
    finally:
        os.close(f)


if __name__ == "__main__":
    sys.exit(main())
