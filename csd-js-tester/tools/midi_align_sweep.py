#!/usr/bin/env python3
"""Alignment sweep: does a filler write re-align snd-usb-audio's USB-MIDI
event packer and revive the diag interface -- WITHOUT touching the driver?

Hypothesis under test. The kernel's rawmidi OUT path for a USB-MIDI device
packs the byte stream into 4-byte USB-MIDI events and keeps a partially filled
event on the SUBSTREAM, not the fd (snd_usb_midi_out stream state: byte/event/
len). So a write whose length is not a multiple of 4 leaves the next write --
from ANY process, because the state lives on the substream -- shifted inside an
event. The device then never sees F0 at an event boundary, the diag parser
never starts a SysEx (csd_diag_midi.xc:112 drops everything before F0), and the
host sees 'first query answered, every later one silent'.

rig/diag_query_sequence.py writes F0,id,<7 text>,F7 = 10 bytes; 10 mod 4 = 2,
so after one such write the packer holds 2 bytes and every later query arrives
rotated by 2. That also explains why a lone F7 (1 byte) did NOT revive it: 1 is
not the complement of 2 mod 4.

Test: for each filler length k in 0..3, write k inert bytes (0xF7) then one
query. Total = k + 10. Some k must satisfy carry + k = 0 mod 4, so if the
hypothesis is right exactly one k (or one k mod 4 class) answers. If NOTHING
answers for any k, the hypothesis is refuted and the fault is elsewhere.

Inert filler: 0xF7 is the SysEx terminator, and the device abandons a message
and resyncs on the next F0 (csd_diag_midi.xc:127-135), so stray F7 bytes cannot
wedge the parser.
"""
import os, re, select, sys, time

CSD_SYSEX_ID = 0x7E          # csd_diag_midi.xc:61 -- NOT 0x43. A wrong
                             # id is abandoned without a reply
                             # (csd_diag_midi.xc:121-123), which reads
                             # as a dead device. Verify constants in
                             # the firmware before writing a probe.
QUERY = b"*IDN?\r\n"


def node_path():
    cards = open("/proc/asound/cards").read()
    m = re.search(r"^\s*(\d+)\s*\[.*\]:.*ClearSound", cards, re.M | re.I)
    if not m:
        sys.exit("no ClearSound card in /proc/asound/cards")
    return f"/dev/snd/midiC{m.group(1)}D0"


def drain(f, secs):
    buf = b""
    t0 = time.time()
    while time.time() - t0 < secs:
        r, _, _ = select.select([f], [], [], 0.2)
        if r:
            try:
                buf += os.read(f, 4096)
            except BlockingIOError:
                pass
    return buf


def write_all(f, data):
    off, deadline = 0, time.time() + 2.0
    while off < len(data):
        n = os.write(f, data[off:])
        if n <= 0:
            if time.time() > deadline:
                raise RuntimeError(f"write stalled at {off}/{len(data)}")
            time.sleep(0.001)
        off += n


def frame(payload):
    return bytes([0xF0, CSD_SYSEX_ID]) + bytes(b & 0x7F for b in payload) + b"\xF7"


def answered(b):
    return b"ClearSound" in b or b"CSD-22" in b


def control_leg():
    """Ask the REFERENCE client (csd_diag.py) whether the interface answers.
    Must run with no rawmidi handle held by us and BEFORE the sweep writes,
    so a silence cannot be blamed on our own writes or on a held fd."""
    import subprocess
    APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
    outs, ran = [], 0
    for i in range(3):
        try:
            # csd_diag.py retries internally; a 25 s wall killed it mid-retry
            # once and produced an exception instead of a result.
            rr = subprocess.run(["./.venv/bin/python", "csd_diag.py", "--backend",
                                 "rawmidi", "--timeout", "6", "*IDN?"],
                                capture_output=True, text=True, cwd=APP, timeout=90)
            o = (rr.stdout or "").strip()
            outs.append(o); ran += 1
            print(f"  csd_diag probe{i+1}: {o or '<silent>'}")
        except subprocess.TimeoutExpired:
            outs.append(""); ran += 1
            print(f"  csd_diag probe{i+1}: <timed out at 90 s>")
        time.sleep(1)
    return outs, ran


p = node_path()
print(f"node: {p}")
print(f"query frame = {len(frame(QUERY))} bytes  ({len(frame(QUERY))} mod 4 = "
      f"{len(frame(QUERY)) % 4})  <- the misalignment this sweep hunts\n")
# CONTROL FIRST, while the substream is untouched by this script's writes and
# no fd of ours is open (a held rawmidi fd makes a second opener get EBUSY and
# fakes a silent device -- that was the third rig bug in this investigation).
print("=== control: reference client, before any write by this script ===")
outs, ran = control_leg()
if ran < 3:
    print("  => control leg INCOMPLETE -- no verdict available.")
    sys.exit(3)
if any(answered(o.encode()) for o in outs):
    print("  => reference client WORKS. The diag interface is alive, so any")
    print("     silence below is a fault of THIS script's wire form, not of")
    print("     the device.")
    sys.exit(0)
print("  => reference client also silent: the interface is genuinely stuck.")
print("     Continuing the sweep to see whether a realigning write revives it.")

f = os.open(p, os.O_RDWR)

hits = []
for k in range(4):
    filler = b"\xF7" * k
    write_all(f, filler)
    time.sleep(0.15)
    write_all(f, frame(QUERY))
    r = drain(f, 3.5)
    ok = answered(r)
    hits.append((k, ok))
    print(f"  filler={k}B (total {k + len(frame(QUERY))}B, mod4={(k + len(frame(QUERY))) % 4}) "
          f"-> {len(r)}B {'ANSWERED' if ok else 'SILENT'} {r[:50]!r}", flush=True)
    time.sleep(0.8)

good = [k for k, ok in hits if ok]
print()
if good:
    print(f"CONFIRMED: queries answer after a {good}-byte filler write.")
    print("  => the fault was 4-byte event misalignment in the HOST's packer,")
    print("     caused by a probe writing a non-multiple-of-4 frame. Firmware")
    print("     was fine. Fix: pad every rawmidi write to a multiple of 4.")
    os.close(f); sys.exit(0)

# Refutation leg: is the interface dead for EVERYONE, or only for raw writes?
print("no filler length revived the raw path.")
os.close(f)
print("  => sweep found no reviving filler length. Combined with the control")
print("     above, the host packer is not the fault: needs a device reset or a")
print("     driver rebind (tools/dfu_exit.py path, or power cycle).")
sys.exit(1)
