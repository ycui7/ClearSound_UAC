#!/usr/bin/env python3
"""Query the diag interface repeatedly on ONE card instance, in the raw byte
form the kernel expects, and print what comes back for each query.

Run this inside an xrun lifetime (scratch/run_xe.sh) with an xSCOPE capture
client attached concurrently, so the CSD_DIAG_TRACE lines from the firmware are
visible in the same window. The pairing is the point: query 1 answers and
query 2+ go silent, and the trace says which side stopped talking.

Wire form: plain MIDI bytes (F0, id, 7-bit text..., F7) written to the rawmidi
node. The KERNEL does the USB-MIDI framing. An earlier probe wrote 4-byte
USB-MIDI event frames here and concluded 'the device is silent at the byte
level' -- wrong conclusion from a wrong wire format: the device never received
a SysEx at all. csd_diag.py's RawMidiTransport.send() (csd_diag.py:558-569) is
the reference for the byte form.
"""
import os, re, select, sys, time

def read_sysex_id():
    """Take the SysEx id from the firmware instead of hard-coding it.

    This is the one constant whose failure fakes a DEAD DEVICE: rx_event
    abandons a message whose id byte mismatches WITHOUT replying
    (csd_diag_midi.xc:121-123). Three probes hard-coded 0x43 and each one
    'proved' the device silent at the byte level. Deriving it at run time
    removes the whole class of bug (and the previous version of this file
    called its own check before defining the constant -> NameError)."""
    src = ("/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac"
           "/src/extensions/csd_diag_midi.xc")
    try:
        txt = open(src, errors="ignore").read()
    except OSError:
        sys.exit(f"cannot read {src} -- refusing to guess the SysEx id")
    m = re.search(r"#define\s+CSD_SYSEX_ID\s+(0x[0-9A-Fa-f]+)", txt)
    if not m:
        sys.exit(f"CSD_SYSEX_ID not found in {src}")
    v = int(m.group(1), 16)
    print(f"CSD_SYSEX_ID read from firmware: 0x{v:02X}", flush=True)
    return v


CSD_SYSEX_ID = read_sysex_id()   # csd_diag_midi.xc:61 today = 0x7E


def node_path():
    cards = open("/proc/asound/cards").read()
    # Accept BOTH product names: 'ClearSound DAC (UAC2.0)' is the RELEASE
    # string, 'XMOS (UAC2.0)' the debug one (xua_defs.h:131-141). Keying on
    # 'ClearSound' alone finds no card on the debug build -- which is the only
    # build that carries CSD_DIAG_TRACE, so it failed on precisely the run that
    # needed it. Same acceptance set as csd_diag.py:495-496.
    m = re.search(r"^\s*(\d+)\s*\[.*\]:.*(ClearSound|XMOS)", cards, re.M | re.I)
    if not m:
        sys.exit("no ClearSound/XMOS card in /proc/asound/cards")
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
    """Same loop as csd_diag.py:558-569 -- a short write on a busy queue would
    otherwise silently truncate a command."""
    off, deadline = 0, time.time() + 2.0
    while off < len(data):
        n = os.write(f, data[off:])
        if n <= 0:
            if time.time() > deadline:
                raise RuntimeError(f"write stalled at {off}/{len(data)}")
            time.sleep(0.001)
            continue
        off += n


def frame(payload):
    """Plain MIDI bytes. NOT 4-byte USB-MIDI events -- see module docstring."""
    body = bytes([0xF0, CSD_SYSEX_ID]) + bytes(b & 0x7F for b in payload) + b"\xF7"
    return body


# Query whatever the caller asks for. Hard-coding *IDN? meant every claim
# about another command (VOLume:TRIM:STATus? in the release notes) had to be
# taken on faith from a client that loses replies -- and it did lose them
# (csd_diag.py returned <silent> while this rig got 7/7 on the same boot).
QUERY = (sys.argv[1] if len(sys.argv) > 1 else "*IDN?").encode() + b"\r\n"


p = node_path()
print(f"node: {p}  query: {QUERY!r}", flush=True)
f = os.open(p, os.O_RDWR)

pre = drain(f, 0.5)
print(f"  pre-drain: {len(pre)} byte(s) {pre[:40]!r}", flush=True)

answered = 0
for q in range(1, 7):
    body = frame(QUERY)
    write_all(f, body)
    r = drain(f, 4)
    ok = bool(r.strip())      # any non-empty reply counts -- the caller may
                              # ask any command, and a trim/status reply does
                              # not contain 'ClearSound'
    answered += 1 if ok else 0
    print(f"  Q{q}: wrote {len(body)}B -> {len(r)}B {'ANSWERED' if ok else 'SILENT'} "
          f"{r[:56]!r}", flush=True)
    time.sleep(1.0)

# A lone F7 tests the snd-usb-audio half-open-message theory in the FORMAT the
# kernel actually parses (F7 as a byte, not as a CIN-encoded event).
print("  --- lone F7, then one more query ---", flush=True)
write_all(f, b"\xF7")
time.sleep(0.4)
_ = drain(f, 0.5)
write_all(f, frame(QUERY))
r = drain(f, 4)
ok = bool(r.strip())
answered += 1 if ok else 0
print(f"  Q-f7: -> {len(r)}B {'ANSWERED' if ok else 'SILENT'} {r[:56]!r}", flush=True)

os.close(f)
print(f"SUMMARY: {answered}/7 answered")
sys.exit(0 if answered else 1)
