#!/usr/bin/env python3
"""Read the MIDI node at the BYTE level, no parsing, no framing assumptions.

Why this exists: csd_diag_midi.xc:216-223 documents, from hardware, that a
SysEx reaching the host WITHOUT its F7 terminator leaves snd-usb-audio inside
an unterminated message, after which 'the device looks completely dead to
every later query -- only the first reply arrives, minus its terminator'.
That is exactly the state we keep hitting (first *IDN? on a fresh card
instance answers, every later one is silent). A parsed client cannot see the
difference between 'device sent nothing' and 'device answered and the kernel
is swallowing it while waiting for a terminator'. Raw bytes can.

Legs:
  A  drain whatever the kernel already buffered (this alone is evidence)
  B  send a lone F7 (terminate anything the kernel thinks is open), then ask
     for *IDN? -- if a reply now appears, the fault is host-side framing
  C  send the query with no F7 and read raw, to show the terminator state of
     a fresh reply
Exit code: 0 if any leg produced an IDN, 1 otherwise.
"""
import glob, re, struct, subprocess, sys, time, fcntl

CSD_SYSEX_ID = 0x43          # matches csd_diag_scpi.c / csd_diag_midi.xc
IDN = b"*IDN?\r\n"


def node():
    for p in sorted(glob.glob("/dev/snd/midiC*D*")):
        pass
    # resolve by card name, not by blind glob: two USB audio devices exist
    cards = open("/proc/asound/cards").read()
    m = re.search(r"^\s*(\d+)\s*\[.*\]:.*ClearSound", cards, re.M | re.I)
    if not m:
        sys.exit("no ClearSound card in /proc/asound/cards")
    c = m.group(1)
    p = f"/dev/snd/midiC{c}D0"
    import os
    if not os.path.exists(p):
        sys.exit(f"{p} missing (driver bound? check amidi -l)")
    return p


def wr(f, data):
    off = 0
    while off < len(data):
        n = os.write(f, data[off:])
        if n <= 0:
            raise RuntimeError("write stalled")
        off += n


def rd_all(f, secs, quiet=False):
    """Return every byte readable in `secs`, as one bytes object."""
    import select
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


def sysex(payload):
    """Frame as our device expects: F0, id, 7-bit data..., F7 -- and hand the
    kernel the USB-MIDI event encoding the driver produces on write."""
    body = bytes([0xF0, CSD_SYSEX_ID]) + bytes(b & 0x7F for b in payload) + b"\xF7"
    return body


def events_from(body):
    """USB Audio 1.0 Table 4-6 encoding, same rule the firmware uses for TX:
    pack up to 3 payload bytes per event; CIN 0x4 continues, the terminal
    event's CIN encodes how many bytes (incl. F7) it carries."""
    ev = []
    i = 0
    while i < len(body):
        chunk = body[i:i + 3]
        i += 3
        b = list(chunk) + [0x00] * (3 - len(chunk))
        if i < len(body):
            ev.append((0x4 << 24) | b[0] << 16 | b[1] << 8 | b[2])
        else:
            n = len(chunk)                      # bytes incl. F7
            cin = {1: 0x5, 2: 0x6, 3: 0x7}[n]
            ev.append((cin << 24) | b[0] << 16 | b[1] << 8 | b[2])
    out = bytearray()
    for e in ev:                                 # little-endian on the wire
        out += bytes((e & 0xFF, (e >> 8) & 0xFF, (e >> 16) & 0xFF, (e >> 24) & 0xFF))
    return bytes(out)


def hexs(b):
    return " ".join(f"{x:02x}" for x in b[:96]) + (" …" if len(b) > 96 else "")


def has_idn(b):
    return b"ClearSound" in b or b"CSD-22" in b


import os
p = node()
print(f"node: {p}")
f = os.open(p, os.O_RDWR)

print("\n[A] drain what the kernel already holds (device sent nothing here)")
b = rd_all(f, 1.5)
print(f"    {len(b)} byte(s): {hexs(b) or '<empty>'}")
if has_idn(b):
    print("    => A REPLY WAS SITTING IN THE KERNEL QUEUE UNPARSED")
    os.close(f); sys.exit(0)

print("\n[B] send a lone F7 to close any half-open SysEx, then ask")
wr(f, events_from(b"\xF7"))
time.sleep(0.3)
wr(f, events_from(sysex(IDN)))
b = rd_all(f, 4)
print(f"    {len(b)} byte(s): {hexs(b) or '<empty>'}")
if has_idn(b):
    print("    => HOST-SIDE FRAMING: closing the message with F7 unblocked it")
    os.close(f); sys.exit(0)

print("\n[C] fresh query, examine terminator of what comes back")
wr(f, events_from(sysex(IDN)))
b = rd_all(f, 4)
print(f"    {len(b)} byte(s): {hexs(b) or '<empty>'}")
if has_idn(b):
    print("    => reply present; terminator present:", b"\xf7" in b)
    os.close(f); sys.exit(0)

print("\nNO REPLY ON ANY LEG (device silent at the byte level)")
os.close(f)
sys.exit(1)
