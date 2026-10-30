#!/usr/bin/env python3
"""Host-side client for the ClearSound UAC SCPI-over-USB-MIDI diagnostic interface.

Wire format (must match firmware):

  * A SCPI line is carried as ONE MIDI SysEx message:
        F0 7E <ASCII bytes of command, no CR/LF> F7
    where 0x7E is a single MMA "Educational & Research" manufacturer ID
    byte.  HARD CONSTRAINT: every byte between F0 and F7 (exclusive) must
    be a 7-bit MIDI data byte (< 0x80); the vendor device-side parser
    (lib_xua midi_in_parse) treats any byte >= 0x80 inside SysEx as a
    status byte and abandons the message, silently dropping the command.
  * On the USB wire the SysEx byte stream is split into 4-byte USB-MIDI
    events: byte0 = (cable<<4)|CIN — the HIGH nibble of byte0 is the
    CABLE NUMBER, the LOW nibble is the CODE INDEX NUMBER (USB Audio 1.0
    Table 4-6).  Vendor proof:
        lib_xua/src/midi/midiinparse.xc makeEvent():
            event = (cable_number << 28) | (codeIndexNumber << 24) | ...
        lib_xua/src/midi/usb_midi.xc (host->device path):
            int event = byterev(datum);   // datum = LE word from USB buf
    Composing the two: wire byte0 high nibble = cable, low nibble = CIN.
    Chunking starts at the F0 itself.  SENT events (USB Audio 1.0
    Table 4-7 spec convention):
        CIN 0x4  SysEx starts/continues, 3 payload bytes
        CIN 0x5  final event, 2 payload bytes (byte3 = 0x00 padding)
        CIN 0x6  final event, 1 payload byte  (byte2/byte3 = 0x00 padding)
        CIN 0x7  final event, 3 payload bytes (last byte = F7)
    The RECEIVER accepts any final CIN 0x5/0x6/0x7 and determines the
    payload length by locating F7 in bytes 1..3 rather than trusting the
    CIN, because the firmware's reply producer uses the vendor convention
    codeIndexNumber = received + 0x4 (midiinparse.xc), i.e. 0x4+k for a
    k-byte tail — which disagrees with the spec convention for k=1 and
    k=2 (a real quirk in the vendor code).
  * Device replies have the same shape (CR/LF stripped from the SCPI text),
    possibly spread over several 64-byte USB-MIDI packets.

Transports:
  * RtMidiTransport  - ALSA sequencer port of the kernel snd-usb-audio
    driver; raw MIDI byte stream (the kernel does USB-MIDI framing).
  * PyUsbTransport   - driverless libusb/pyusb access to the class-compliant
    bulk MIDI endpoints; this module does the USB-MIDI framing itself.
  * SimTransport     - in-process fake device (firmware_sim.py), used by
    the test suite and ``--sim``.

CLI examples:
    csd_diag.py --list
    csd_diag.py --sim '*IDN?'
    csd_diag.py --sim --repl
    csd_diag.py --sim 'REGister:READ? "AA=="'
    csd_diag.py --backend pyusb --pid 0x0018 'REGister:WRITE "Ayo="'
    # (or use the helpers: client.reg_read(0x00) / client.reg_write({3: 0x2A}))
    csd_diag.py --backend rtmidi --port 'XMOS' commands.txt
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Protocol

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

MANUFACTURER_ID = 0x7E      # single SysEx ID byte: MMA "Educational & Research"
                            # (7-bit-safe; the vendor device parser treats any
                            # byte >=0x80 inside SysEx as a status byte)
SYSEX_START = 0xF0
SYSEX_END = 0xF7
CABLE_NUMBER = 0x00

CIN_SYSEX_3 = 0x4        # SysEx starts / continues: 3 payload bytes
CIN_SYSEX_END_2 = 0x5    # SysEx ends, 2 payload bytes (USB Audio 1.0 spec)
CIN_SYSEX_END_1 = 0x6    # SysEx ends, 1 payload byte  (USB Audio 1.0 spec)
CIN_SYSEX_FULL3 = 0x7    # complete SysEx, 3 payload bytes (ends with F7)

DEFAULT_TIMEOUT = 1.0    # seconds waiting for a device reply
USB_PACKET_SIZE = 64     # full-speed AudioClass MIDI bulk packet size


class DiagError(Exception):
    """Base error for the diagnostic client."""


class DiagTimeout(DiagError):
    """No complete SysEx reply received before the timeout."""


class DeviceError(DiagError):
    """Device replied with a SCPI error line ('-<code>,<msg>' or 'ERROR: ...')."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


_SCPI_ERR_RE = None  # compiled lazily below


def _is_error_reply(text: str) -> bool:
    """True if reply text is a SCPI error ('-201,...') or legacy 'ERROR: ...'."""
    global _SCPI_ERR_RE
    if _SCPI_ERR_RE is None:
        import re
        _SCPI_ERR_RE = re.compile(r"^-\d+,")
    return bool(_SCPI_ERR_RE.match(text)) or text.startswith("ERROR")


# --------------------------------------------------------------------------
# Wire primitives
# --------------------------------------------------------------------------

def build_sysex(command: str) -> bytes:
    """Wrap a SCPI command line into a full SysEx message.

    Returns ``F0 7E <ascii cmd> F7`` (CR/LF stripped).  Every byte between
    the F0/F7 delimiters is a 7-bit MIDI data byte; commands containing
    non-ASCII (>0x7F) characters are rejected client-side with DiagError
    (the device parser would otherwise drop the message).
    """
    stripped = command.replace("\r", "").replace("\n", "")
    try:
        payload = stripped.encode("ascii")
    except UnicodeEncodeError:
        raise DiagError("SCPI command must be 7-bit ASCII") from None
    if any(b >= 0x80 for b in payload):           # belt-and-braces
        raise DiagError("SCPI command contains non-7-bit byte")
    if not payload:
        raise DiagError("empty SCPI command")
    if len(payload) > 512:
        raise DiagError("SCPI command too long (>512 bytes)")
    return bytes([SYSEX_START, MANUFACTURER_ID]) + payload + bytes([SYSEX_END])


def parse_sysex(msg: bytes) -> str:
    """Validate a complete SysEx message and return the SCPI text.

    Raises DiagError if framing or the manufacturer ID byte is wrong.
    """
    if len(msg) < 4 or msg[0] != SYSEX_START or msg[-1] != SYSEX_END:
        raise DiagError(f"not a SysEx message: {msg!r}")
    if msg[1] != MANUFACTURER_ID:
        raise DiagError(f"unexpected manufacturer ID: {msg[1]:02x}")
    return msg[2:-1].decode("ascii", errors="replace")


def pack_events(sysex: bytes) -> List[bytes]:
    """Split a complete SysEx message into a list of 4-byte USB-MIDI events.

    byte0 = (CABLE_NUMBER << 4) | CIN: the HIGH nibble is the cable number,
    the LOW nibble the code index number (USB Audio 1.0 Table 4-6).  This
    is fixed by the vendor source, not interpretation:
        lib_xua/src/midi/midiinparse.xc makeEvent():
            event = (cable_number << 28) | (codeIndexNumber << 24) | ...
        lib_xua/src/midi/usb_midi.xc (host->device path):
            int event = byterev(datum);  // datum = LE word from USB buffer
    which composes to: wire byte0 high nibble = cable, low nibble = CIN.
    Uses CIN 0x4 for full 3-byte chunks and the USB Audio 1.0 Table 4-7
    spec final CINs: 0x5 = ends with 2 data bytes, 0x6 = ends with 1,
    0x7 = ends with 3.
    """
    events: List[bytes] = []
    i, n = 0, len(sysex)
    while i < n:
        rem = n - i
        if rem > 3:
            chunk, cin = sysex[i:i + 3], CIN_SYSEX_3
            i += 3
        else:
            chunk = sysex[i:]
            cin = {1: CIN_SYSEX_END_1, 2: CIN_SYSEX_END_2, 3: CIN_SYSEX_FULL3}[rem]
            i = n
        pad = chunk + bytes(3 - len(chunk))
        events.append(bytes([(CABLE_NUMBER << 4) | cin, pad[0], pad[1], pad[2]]))
    return events


def pack_packets(sysex: bytes, max_packet: int = USB_PACKET_SIZE) -> List[bytes]:
    """Group SysEx events into USB bulk packets of at most *max_packet* bytes."""
    events = pack_events(sysex)
    per = max(1, max_packet // 4)
    return [b"".join(events[i:i + per]) for i in range(0, len(events), per)]


class SysexCollector:
    """Reassembles complete raw MIDI SysEx messages from a MIDI byte stream.

    Used for the rtmidi/ALSA path where the kernel already removed the
    USB-MIDI framing.  Non-SysEx status bytes are ignored (real-time
    messages may legally interleave SysEx).
    """

    def __init__(self) -> None:
        self._buf: Optional[bytearray] = None

    def feed(self, data: bytes) -> List[bytes]:
        """Feed raw MIDI bytes; return list of complete F0..F7 messages."""
        out: List[bytes] = []
        for b in data:
            if b == SYSEX_START:
                self._buf = bytearray([b])          # restart on stray F0
            elif self._buf is not None:
                if b == SYSEX_END:
                    self._buf.append(b)
                    out.append(bytes(self._buf))
                    self._buf = None
                elif b >= 0xF8:
                    pass                              # realtime: interleaved, skip
                elif b == SYSEX_START:
                    self._buf = bytearray([b])        # restart on stray F0
                else:
                    # Inside SysEx every other byte is payload - including
                    # 0x80-0xEF values such as manufacturer ID byte 0xB1.
                    self._buf.append(b)
                    if len(self._buf) > 1024:         # runaway guard
                        self._buf = None
        return out


    def flush(self) -> List[bytes]:
        """Emit any pending bytes as a complete message.

        The kernel's rawmidi node hands us one SysEx per read batch and drops
        the terminating F7 -- observed directly: writing F0 7E '*IDN?' F7 got
        back 34 bytes ending '0d 0a' with no F7. So for that transport
        end-of-read means end-of-message, and waiting for F7 would hang until
        the timeout. The F7 is restored here so parse_sysex() still validates
        the frame.
        """
        out: List[bytes] = []
        if self._buf is not None and len(self._buf) > 1:
            self._buf.append(SYSEX_END)
            out.append(bytes(self._buf))
        self._buf = None
        return out

    def pending(self) -> bool:
        return self._buf is not None and len(self._buf) > 1


class UsbMidiEventParser:
    """Incremental USB-MIDI packet parser producing complete SysEx messages.

    Feed it the raw byte stream of bulk-IN data (4-byte events, zero
    padded); it tolerates events split across arbitrary read boundaries
    and accepts final events under either the USB Audio 1.0 spec CIN
    convention (0x5=2 bytes, 0x6=1 byte, 0x7=3 bytes) or the vendor
    producer convention (codeIndexNumber = received + 0x4), locating F7
    in the event payload to decide the message end.
    """

    def __init__(self) -> None:
        self._pending = bytearray()
        self._msg = bytearray()
        self._in_sysex = False

    def feed(self, data: bytes) -> List[bytes]:
        """Feed raw bulk-IN bytes; return complete SysEx messages (bytes)."""
        self._pending.extend(data)
        out: List[bytes] = []
        while len(self._pending) >= 4:
            ev = bytes(self._pending[:4])
            del self._pending[:4]
            msg = self._feed_event(ev)
            if msg is not None:
                out.append(msg)
        return out

    def _feed_event(self, ev: bytes) -> Optional[bytes]:
        # byte0 high nibble = CABLE NUMBER, low nibble = CODE INDEX NUMBER
        # (USB Audio 1.0 Table 4-6; fixed by lib_xua midiinparse.xc
        #  makeEvent(): event = (cable_number << 28) | (codeIndexNumber << 24) | ...
        #  combined with usb_midi.xc host->device `int event = byterev(datum)`).
        cable = (ev[0] >> 4) & 0x0F
        cin = ev[0] & 0x0F
        if cable != CABLE_NUMBER or cin not in (
                CIN_SYSEX_3, CIN_SYSEX_END_1, CIN_SYSEX_END_2, CIN_SYSEX_FULL3):
            return None                              # unused / non-SysEx event
        data = ev[1:]
        if cin == CIN_SYSEX_3:
            if not self._in_sysex:
                if data[0] != SYSEX_START:
                    return None                      # continuation without start
                self._msg = bytearray()
                self._in_sysex = True
            self._msg.extend(data)
            return None
        # final event of a SysEx message: accept ANY final CIN 0x5/0x6/0x7
        # (USB Audio 1.0 Table 4-7 spec convention AND the vendor reply
        # producer's codeIndexNumber = received + 0x4 convention from
        # lib_xua midiinparse.xc — they disagree for 1- and 2-byte tails),
        # and determine the payload length by LOCATING F7 in bytes 1..3
        # rather than trusting the CIN.
        if not self._in_sysex:
            return None
        f7 = data.find(SYSEX_END)
        if f7 >= 0:
            self._msg.extend(data[:f7 + 1])
        else:
            # malformed final event (no F7): leniently take the bytes
            # before the zero padding; the message stays open until an F7.
            self._msg.extend(data.rstrip(b"\x00"))
        # complete at the first F7 (a stray F0 earlier restarts the message)
        for idx, b in enumerate(self._msg):
            if b == SYSEX_END:
                msg = bytes(self._msg[:idx + 1])
                self._msg = bytearray()
                self._in_sysex = False
                return msg
            if b == SYSEX_START and idx > 0:
                self._msg = self._msg[idx:]          # restart, keep new header
                break
        return None


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------

class Transport(Protocol):
    """Byte-pipe transport to the device's MIDI interface."""

    def send(self, data: bytes) -> None:
        """Send raw bytes (already wire-formatted for this transport)."""
        ...

    def recv(self, size: int, timeout: float) -> bytes:
        """Return up to *size* bytes, or b'' if nothing arrived in *timeout*."""
        ...

    def close(self) -> None:
        """Release the device/port."""
        ...


class RtMidiTransport:
    """Driver-side transport via python-rtmidi (ALSA sequencer raw bytes).

    The kernel's snd-usb-audio driver performs the USB-MIDI packetisation,
    so this transport exchanges *raw* MIDI bytes (F0 ... F7).
    """

    def __init__(self, port: str, open_timeout: float = 0.5):
        import rtmidi  # imported lazily so the module works without it
        self._rtmidi = rtmidi
        self._out = rtmidi.MidiOut()
        self._in = rtmidi.MidiIn()
        self._in.cancel_callback()
        # python-rtmidi filters SysEx out by default ("...because they can fill
        # up input buffers very quickly"), which would silently discard every
        # reply. The attribute is not `ignore_sysex`; the real API is
        # ignore_types(sysex, timing, active_sense) -- verified against
        # rtmidi.MidiIn on this machine.
        self._in.ignore_types(sysex=False, timing=True, active_sense=True)
        self._out_port = self._match(self._out.get_ports(), port)
        self._in_port = self._match(self._in.get_ports(), port)
        self._out.open_port(self._out_port)
        self._in.open_port(self._in_port)

    @staticmethod
    def _match(ports: List[str], needle: str) -> int:
        needle_l = needle.lower()
        for i, p in enumerate(ports):
            if needle_l in p.lower():
                return i
        raise DiagError(f"no MIDI port matching {needle!r}; ports: {ports}")

    def send(self, data: bytes) -> None:
        self._out.send_message(list(data))

    def recv(self, size: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        out = bytearray()
        while time.monotonic() < deadline:
            msg = self._in.get_message()
            if msg is None:
                time.sleep(0.002)
                continue
            out.extend(bytes(msg[0])[:size])
            if len(out) >= size:
                break
        return bytes(out)

    def close(self) -> None:
        self._out.close_port()
        self._in.close_port()


def find_midi_bulk_endpoints(config_desc: bytes) -> tuple:
    """Parse a raw USB config descriptor; return (bulk_out_addr, bulk_in_addr).

    A MIDI data endpoint is an ENDPOINT descriptor (type 0x05, bulk)
    immediately followed by a class-specific CS_ENDPOINT (type 0x25) with
    subtype 0x01 (MS_GENERAL), per USB Audio 1.0 / USB-MIDI 1.0.
    """
    ep_out = ep_in = None
    i, n = 0, len(config_desc)
    prev: Optional[tuple] = None  # (addr, attributes) of last endpoint seen
    while i + 2 <= n:
        blen = config_desc[i]
        btype = config_desc[i + 1]
        if blen < 2 or i + blen > n:
            break
        if btype == 0x05 and blen >= 7:              # ENDPOINT
            addr = config_desc[i + 2]
            attr = config_desc[i + 3] & 0x03
            prev = (addr, attr) if attr == 0x02 else None
        elif btype == 0x25 and blen >= 3 and prev is not None \
                and config_desc[i + 2] == 0x01:      # CS_ENDPOINT / MS_GENERAL
            addr, _ = prev
            if addr & 0x80:
                ep_in = addr
            else:
                ep_out = addr
        i += blen
    return ep_out, ep_in


def _config_bytes(dev) -> bytes:
    """Full configuration descriptor, via GET_DESCRIPTOR.

    pyusb 1.3.1 has no Device.get_descriptor(), and the parsed tree it does
    offer loses the byte layout we need to spot a CS_ENDPOINT following a bulk
    ENDPOINT. A control transfer gives the exact bytes. Two requests: 9 bytes
    for wTotalLength, then the whole thing.
    """
    head = dev.ctrl_transfer(0x80, 0x06, 0x0200, 0x0000, 9)
    total = head[2] | (head[3] << 8)
    full = dev.ctrl_transfer(0x80, 0x06, 0x0200, 0x0000, total)
    return bytes(full)


def _interface_for_endpoints(config_desc: bytes, ep_out, ep_in) -> int:
    """Interface number that owns the given bulk endpoints.

    The MIDI bulk endpoints sit on the MIDI Streaming interface, and lib_xua
    puts them under alt 0 -- not on interface 0. Detaching the kernel from the
    wrong interface leaves it attached to the one that matters and every read
    then returns nothing. Walk the descriptor and return the bInterfaceNumber
    in force when either endpoint appears; 4 is the value seen on hardware.
    """
    want = {a for a in (ep_out, ep_in) if a is not None}
    cur = 0
    i, n = 0, len(config_desc)
    while i + 2 <= n:
        blen = config_desc[i]
        btype = config_desc[i + 1]
        if blen < 2 or i + blen > n:
            break
        if btype == 0x04 and blen >= 4:                      # INTERFACE
            cur = config_desc[i + 2]
        elif btype == 0x05 and blen >= 4 and (config_desc[i + 3] & 0x03) == 0x02:
            if config_desc[i + 2] in want:
                return cur
        i += blen
    return 0


class RawMidiTransport:
    """Transport through the kernel raw MIDI node (/dev/snd/midiC<D>D<P>).

    This is the path that actually works against the ClearSound DAC, and the
    reason it exists is a real bug we hit, not a preference:

      python-rtmidi talks to the ALSA *sequencer*. On this device the sequencer
      port appears ("XMOS (UAC2.0) MIDI 1", RWeX) and writes into it succeed,
      but the kernel never submits an IN URB for the MIDI bulk endpoint, so
      every reply is lost and the host just times out. amidi -d and aseqdump
      both saw nothing while the device was provably transmitting: the firmware
      trace showed ep_buffer arming the endpoint and XUD reporting the transfer
      complete.

      Writing /dev/snd/midiC4D0 directly (the rawmidi node, not the sequencer)
      round-trips first time: 8 bytes out, 34 back, 'ClearSound,CSD-22,...'.

    So this is the default backend, and the sequencer path is kept only as an
    option for hosts where it happens to work.

    The kernel performs USB-MIDI packetisation here, so we exchange *raw* MIDI
    bytes (F0 <id> <text> F7) -- same framing as the rtmidi backend.
    """

    def __init__(self, needle: str = "XMOS", timeout: float = 4.0):
        import glob
        self._fd = None
        node = self._find(needle)
        if node is None:
            raise DiagError(
                "no raw MIDI node for %r (looked in /dev/snd/midiC*D*; check "
                "'amidi -l' and that the device is enumerated)" % needle)
        self.node = node
        self._fd = os.open(node, os.O_RDWR)

    @staticmethod
    def _card_names() -> dict:
        """card number -> all the text ALSA prints for it, lowercase.

        /proc/asound/cards looks like

             4 [XMOSUAC20      ]: USB-Audio - XMOS (UAC2.0)
                                  XMOS XMOS (UAC2.0) at usb-..., high speed

        The card number and the short name sit inside/just before the brackets,
        so the number must be taken from BEFORE '[' -- splitting on ']' first
        and int()-ing the remainder ("4 [XMOSUAC20") raises, which used to
        silently leave the map empty and make discovery report "no raw MIDI
        node" for a device that was plainly present. The continuation line is
        folded in as well: it carries the longer model string.
        """
        import re
        names = {}
        try:
            with open("/proc/asound/cards") as f:
                lines = f.read().splitlines()
        except OSError:
            return {}
        cur = None
        for line in lines:
            m = re.match(r"\s*(\d+)\s+\[([^\]]*)\]\s*:?\s*(.*)", line)
            if m:
                cur = int(m.group(1))
                names[cur] = "%s %s" % (m.group(2), m.group(3))
                continue
            if cur is not None and line.strip():
                names[cur] += " " + line.strip()
        return {k: v.lower() for k, v in names.items()}

    @classmethod
    def _find(cls, needle: str):
        import glob, re
        names = cls._card_names()
        cand = []
        for path in sorted(glob.glob("/dev/snd/midiC*D*")):
            m = re.search(r"midiC(\d+)D(\d+)$", path)
            if not m:
                continue
            card = int(m.group(1))
            label = names.get(card, "")
            if needle.lower() in label or needle.lower() in path.lower():
                cand.append((0 if "xmos" in label else 1, path))
        if not cand:
            return None
        cand.sort()
        return cand[0][1]

    def send(self, data: bytes) -> None:
        # A rawmidi write may be short on a busy queue; loop until everything
        # is accepted, or a stalled queue would silently truncate a command.
        off = 0
        deadline = time.time() + 2.0
        while off < len(data):
            n = os.write(self._fd, data[off:])
            if n <= 0:
                if time.time() > deadline:
                    raise DiagError("rawmidi write stalled at %d/%d" % (off, len(data)))
                time.sleep(0.001)
                continue
            off += n

    def recv(self, size: int, timeout: float) -> bytes:
        import select
        r, _, _ = select.select([self._fd], [], [], timeout)
        if not r:
            return b""
        try:
            return os.read(self._fd, size)
        except OSError:
            return b""

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None


class PyUsbTransport:
    """Transport straight to the USB-MIDI bulk endpoints, via pyusb.

    This is the primary transport, chosen on evidence rather than taste. The
    kernel's MIDI path (snd-usb-audio -> rawmidi/sequencer) delivered the first
    reply after a fresh bind and then went quiet: the device log showed the
    whole reply transmitted and XUD reporting every transfer complete, while
    amidi, aseqdump, rtmidi and direct /dev/snd reads returned nothing. Talking
    to the endpoints directly round-trips every time and shows the exact wire
    bytes, so it is also the right tool for debugging the interface itself.

    Needs the kernel off the MIDI Streaming interface, so either root or a udev
    rule (see README). Only that interface is detached -- pulling the audio
    interfaces off the kernel would kill the stream and is not needed.

    *finder* is injectable for testing.
    """

    def __init__(self, vid: int, pid: int, finder=None):
        import usb.core  # lazily
        self._usb_core = usb.core
        find = finder or usb.core.find
        self._dev = find(idVendor=vid, idProduct=pid)
        if self._dev is None:
            raise DiagError("USB device %04x:%04x not found" % (vid, pid))
        dev = self._dev

        # libusb recipe, established against this device by trial:
        #   detach the kernel from the MIDI Streaming interface, then CLAIM it.
        #
        # Two mistakes here produce identical-looking EIO failures, and dmesg
        # tells them apart:
        #   "usbfs: interface 0 claimed by snd-usb-audio while 'python' sets
        #    config #1"  <- caused by calling set_configuration() while the
        #                    kernel still holds other interfaces of this
        #                    configuration. We therefore never call it; the
        #                    kernel already configured the device.
        #   "usbfs: usb_submit_urb returned -2" (ENOENT) <- caused by
        #                    transferring on an interface that is detached but
        #                    not claimed. Hence claim_interface below.
        # If a previous run died mid-session it leaves the interface detached,
        # so re-attach the driver first to get back to a known state.
        try:
            cfg = dev.get_active_configuration()
        except Exception:
            # Raised as "Configuration not set" when the driver was unbound
            # from under us (see xmos_ownership.sh). With no driver at all on
            # the device it is safe -- and necessary -- to configure it here.
            try:
                dev.set_configuration()
                cfg = dev.get_active_configuration()
            except Exception as e:
                raise DiagError(
                    "device is not configured and cannot be configured: %s "
                    "(is snd-usb-audio bound? try: sudo bash "
                    "~/.hermes/cache/scratch/xmos_ownership.sh bind)" % e)
        self._ifnum, self._alt, self._ep_out, self._ep_in = self._from_tree(cfg)
        if self._ep_out is None or self._ep_in is None:
            try:
                raw = _config_bytes(dev)
                self._ep_out, self._ep_in = find_midi_bulk_endpoints(raw)
                self._ifnum = _interface_for_endpoints(raw, self._ep_out,
                                                       self._ep_in)
                self._alt = 0
            except Exception:
                pass
        if self._ep_out is None or self._ep_in is None:
            raise DiagError("no USB-MIDI bulk endpoints found on device")

        import usb.util
        dbg = os.environ.get("CSD_DIAG_DEBUG")
        def say(fmt, *a):
            if dbg:
                print("[pyusb] " + fmt % a, file=sys.stderr)

        # The sequence proven against this hardware is short: the kernel owns
        # the device (it just enumerated, snd-usb-audio bound), we detach only
        # the MIDI Streaming interface, then transfer.
        #
        # Two things were tried and are NOT done, both because they make every
        # transfer fail with EIO (kernel: "usb_submit_urb returned -2"):
        #   - set_configuration(): while snd-usb-audio holds the audio
        #     interfaces, re-setting the configuration breaks the pipes.
        #   - attach_kernel_driver(): re-attaching starts a fresh snd-usb-audio
        #     probe of the whole device which does not complete cleanly, and
        #     the configuration is left unusable.
        #
        # "Already detached" is the NORMAL state for the second and later runs
        # of this tool, not an error: close() releases our claim but nothing
        # re-attaches the kernel (see close()), and a process killed mid-run
        # leaves it detached too. An earlier version refused here, so only the
        # very first run after enumeration ever worked and every retry died
        # with a message blaming a crash. Nothing owns the interface in that
        # state, so proceed and let the claim below report a real conflict.
        try:
            active = dev.is_kernel_driver_active(self._ifnum)
        except Exception as e:
            say("is_kernel_driver_active(%d) -> %s", self._ifnum, e)
            active = True
        say("if%d kernel_driver_active=%s", self._ifnum, active)
        if active is False:
            say("if%d already detached (earlier run); claiming directly", self._ifnum)
        else:
            try:
                dev.detach_kernel_driver(self._ifnum)
                say("detached driver on if%d", self._ifnum)
            except Exception as e:
                say("detach_kernel_driver(%d) -> %s", self._ifnum, e)
        self._claimed = False
        # No set_interface_altsetting() here. The endpoints live under alt 0 and
        # alt 0 is what the kernel already selected, so there is nothing to
        # change; and once we have detached, libusb auto-claims the interface
        # for the transfer. Re-setting the alt setting at that point tears the
        # auto-claim down (alt 0 of the AudioControl/MIDI pair declares its
        # endpoints conditionally) and the next bulk transfer fails with EIO.
        # The probe that round-trips does exactly: detach, transfer.

    @staticmethod
    def _from_tree(cfg):
        """(interface, alt, bulk_out, bulk_in) from the parsed config tree.

        The MIDI Streaming interface is class 1 / subclass 3, and its bulk pair
        is what carries SysEx. Picking it by class is exact and avoids guessing
        which of the audio interfaces' bulk endpoints, if any, is MIDI. We
        return the alternate setting the endpoints were found under -- on this
        device that is alt 0, and selecting a different alt would disable them.
        """
        for intf in cfg:
            if intf.bInterfaceClass != 1 or intf.bInterfaceSubClass != 3:
                continue
            ep_out = ep_in = None
            for ep in intf:
                if (ep.bmAttributes & 0x03) != 0x02:      # not bulk
                    continue
                if ep.bEndpointAddress & 0x80:
                    ep_in = ep.bEndpointAddress
                else:
                    ep_out = ep.bEndpointAddress
            if ep_out is not None and ep_in is not None:
                return (intf.bInterfaceNumber, intf.bAlternateSetting,
                        ep_out, ep_in)
        return 0, 0, None, None

    def send(self, data: bytes) -> None:
        # One bulk transfer for the whole message, as the working probe did.
        # Writing one 4-byte event at a time is legal, but each transfer is a
        # separate NAK/ACK round trip and the device's event collector is
        # easier to reason about when a message arrives whole.
        if not data:
            return
        self._dev.write(self._ep_out, list(data), 3000)

    def recv(self, size: int, timeout: float) -> bytes:
        ms = max(1, int(timeout * 1000))
        try:
            return bytes(self._dev.read(self._ep_in, size, timeout=ms))
        except self._usb_core.USBError as e:
            # 110 = LIBUSB_ERROR_TIMEOUT: no reply in this window. Callers poll
            # until their own deadline, so this is normal, not an error.
            if e.errno not in (110,):
                raise
            return b""

    def close(self) -> None:
        import usb.util
        # Do NOT attach_kernel_driver() here. Re-attaching makes snd-usb-audio
        # start a fresh probe of the whole device, which does not complete
        # cleanly while we still hold the configuration, and leaves the device
        # unusable until it re-enumerates. The clean handback is a re-run of
        # the firmware (or xmos_ownership.sh bind), which resets USB.
        try:
            if getattr(self, "_claimed", False):
                usb.util.release_interface(self._dev, self._ifnum)
                self._claimed = False
        except Exception:
            pass
        try:
            usb.util.dispose_resources(self._dev)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class CsdDiag:
    """Request/response SCPI client over a MIDI Transport.

    *framing* selects how bytes on the transport map to SysEx:
      "usb-midi" - 4-byte USB-MIDI event packets (pyusb, simulator);
      "raw"      - raw MIDI byte stream (rtmidi/ALSA).
    """

    def __init__(self, transport: Transport, framing: str = "usb-midi"):
        if framing not in ("usb-midi", "raw"):
            raise ValueError(f"unknown framing {framing!r}")
        self._t = transport
        self._framing = framing
        self._events = UsbMidiEventParser()
        self._raw = SysexCollector()

    def close(self) -> None:
        self._t.close()

    def _flush(self) -> None:
        """Drop stale bytes/messages pending from a previous exchange.

        Best-effort by definition, so it swallows transport errors. That is not
        sloppy: on the bulk path a read with a tiny timeout gets cancelled
        mid-transfer, and libusb can report that as EIO rather than a timeout.
        Treating it as fatal made the very first query of a session die in the
        flush, before a single command was sent.
        """
        try:
            while self._t.recv(4096, 0.0):
                pass
        except Exception:
            pass
        if self._framing == "usb-midi":
            self._events = UsbMidiEventParser()
        else:
            self._raw = SysexCollector()

    def send_command(self, command: str) -> None:
        """Send one SCPI command as a single SysEx message."""
        sysex = build_sysex(command)
        if self._framing == "usb-midi":
            self._t.send(b"".join(pack_packets(sysex)))
        else:
            self._t.send(sysex)

    def read_reply(self, timeout: float = DEFAULT_TIMEOUT,
                   raise_error: bool = True) -> str:
        """Wait for one complete reply SysEx; return the SCPI text.

        Raises DiagTimeout on timeout.  On a SCPI error reply
        ('-<code>,<msg>' or legacy 'ERROR: ...') raises DeviceError unless
        *raise_error* is False, in which case the error text is returned.
        """
        collector = self._events if self._framing == "usb-midi" else self._raw
        # A rawmidi/sequencer read returns whatever the kernel has buffered, so
        # one poll is not enough: an empty read means "not yet", not "no reply".
        # The old loop treated an empty chunk as failure and gave up
        # immediately, which made queries time out unless the reply happened to
        # already be queued when we asked -- the device had in fact answered
        # (firmware trace: "parse rc=1 wrote=32 ring=1") while the client
        # reported TIMEOUT. Poll until the deadline instead; select() blocks, so
        # this is not a busy wait.
        want = USB_PACKET_SIZE if self._framing == "usb-midi" else 256
        deadline = time.monotonic() + timeout
        # For the raw (kernel rawmidi) path a reply may arrive in one read with
        # its F7 stripped, or split across reads, so "no more bytes for a short
        # while" is what closes a message there. Kept separate from the overall
        # timeout so a slow device still gets its full budget on the first
        # chunk.
        QUIESCE = 0.05
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DiagTimeout("no reply within timeout")
            chunk = self._t.recv(want, remaining)
            for msg in collector.feed(chunk):
                try:
                    text = parse_sysex(msg)
                except DiagError:
                    continue                        # ignore foreign messages
                if raise_error and _is_error_reply(text):
                    raise DeviceError(text)
                return text
            if chunk and self._framing != "usb-midi" and collector.pending():
                # Keep draining until the stream goes quiet, then close it.
                while True:
                    r = self._t.recv(want, QUIESCE)
                    if not r:
                        break
                    for msg in collector.feed(r):
                        try:
                            text = parse_sysex(msg)
                        except DiagError:
                            continue
                        if raise_error and _is_error_reply(text):
                            raise DeviceError(text)
                        return text
                for msg in collector.flush():
                    try:
                        text = parse_sysex(msg)
                    except DiagError:
                        continue
                    if raise_error and _is_error_reply(text):
                        raise DeviceError(text)
                    return text

    def query(self, command: str, timeout: float = DEFAULT_TIMEOUT,
              raise_error: bool = True) -> str:
        """Send a SCPI query and return the device's reply text."""
        self._flush()
        self.send_command(command)
        return self.read_reply(timeout, raise_error=raise_error)

    def command(self, command: str) -> None:
        """Send a SCPI set-command; the device answers nothing (no wait)."""
        self._flush()
        self.send_command(command)

    # ---- codec registers (base64 payload, BATMCU convention) -------------
    #
    # Wire form, matching the proven pattern in the specialtycircuits repo
    # (stm32core/BATMCU_R8_F401RCT6/.dsh/skills/batmcu-spi-bringup/SKILL.md):
    #     DIAGnostic:SPI0:CS1:WRB64? "<base64>"
    #     DIAGnostic:SPI0:CS1:WB64   "<base64>"
    # Payloads are lists, so a register range costs one round trip instead of
    # one per register:
    #     REGister:READ?  "<b64 of [addr,...]>"   -> "<b64 of [val,...]>"
    #     REGister:WRITE  "<b64 of [addr,val,...]>"   (silent)
    #
    # Base64 rather than hex text because libscpi's lexer rejects some hex
    # spellings before the callback runs -- measured through the native
    # harness: "0xFF" and "0x3" are accepted, "0x03" and "0X03" return -101
    # Invalid character. A bench command whose syntax depends on the number of
    # digits in the value is not usable.
    #
    # Replies are read with the same discipline the BATMCU harness records:
    # take the LAST quoted string, never the first. Its documented trap is
    # that base64 of the literal "ACK" decodes to 00 22 -- a ghost payload
    # that would read back as a plausible register value of 0.

    # The device emits the payload RAW: SCPI_ResultCharacters writes the
    # characters with no surrounding quotes (measured on hardware: the reply
    # to REGister:READ? [0x00,0x03] is the four bytes AEA=\\r\\n; the host
    # harness shows the same for Wg==). Accepting only a "quoted" form -- the
    # BATMCU convention, whose firmware used SCPI_ResultQuotedCharacters --
    # made every register read fail on the real device while the sim, which
    # quoted, stayed green. The parser therefore strips optional quotes
    # rather than requiring them: whichever C++ variant ships, the client
    # decodes, and an error reply still fails base64 validation (quotes,
    # commas and spaces are not in the alphabet), so errors cannot be
    # mistaken for payload.
    _BARE_B64 = re.compile(r"[A-Za-z0-9+/=]*")

    @classmethod
    def _payload_from_reply(cls, reply: str) -> bytes:
        s = reply.strip()
        if s.startswith('"') and s.endswith('"') and len(s) >= 2:
            s = s[1:-1]                      # tolerate a quoted variant
        if not s or not cls._BARE_B64.fullmatch(s):
            raise DiagError("register read reply is not base64: %r" % reply)
        try:
            return base64.b64decode(s, validate=True)
        except Exception as e:
            raise DiagError("register reply is not valid base64: %s (%r)" % (e, reply))

    def reg_read(self, addrs, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        """Read codec registers. `addrs` is an int or a list of ints (0..0xFF).

        Returns the values, positionally aligned with the addresses. Raises
        DiagError on a malformed reply: an empty or short payload is reported,
        never padded, because a silently short register dump decodes as a
        plausible-looking map of a codec we did not actually read.
        """
        if isinstance(addrs, int):
            addrs = [addrs]
        for a in addrs:
            if not 0 <= a <= 0xFF:
                raise DiagError("register address 0x%02x out of range" % a)
        payload = base64.b64encode(bytes(addrs)).decode()
        reply = self.query('REGister:READ? "%s"' % payload, timeout=timeout)
        data = self._payload_from_reply(reply)
        if len(data) != len(addrs):
            raise DiagError("register reply short: asked %d, got %d"
                            % (len(addrs), len(data)))
        return data

    def reg_read_map(self, lo: int, hi: int) -> dict:
        """Contiguous register range as {addr: value} -- one round trip."""
        if not (0 <= lo <= hi <= 0xFF):
            raise DiagError("bad range 0x%02x..0x%02x" % (lo, hi))
        addrs = list(range(lo, hi + 1))
        if len(addrs) > 64:
            raise DiagError("range too large for one command (%d > 64)" % len(addrs))
        return dict(zip(addrs, self.reg_read(addrs)))

    def reg_write(self, pairs) -> None:
        """Write codec registers. `pairs` is {addr: value} or [(addr, value), ...].

        Set-commands are silent by SCPI convention, so this returns nothing and
        raises on a transport error only; a rejected command shows up in the
        error queue, which the caller reads with system_error_next(). Writes
        are applied in order and a mid-list I2C failure leaves the earlier
        ones written -- the codec has no dry-run, so read back to confirm.
        """
        items = pairs.items() if isinstance(pairs, dict) else pairs
        buf = bytearray()
        for a, v in items:
            if not 0 <= a <= 0xFF:
                raise DiagError("register address 0x%02x out of range" % a)
            if not 0 <= v <= 0xFF:
                raise DiagError("register value 0x%02x out of range" % v)
            buf += bytes((a, v))
        if not buf:
            raise DiagError("no registers to write")
        if len(buf) > 128:
            raise DiagError("payload too large (%d bytes > 128)" % len(buf))
        self.command('REGister:WRITE "%s"' % base64.b64encode(bytes(buf)).decode())


# --------------------------------------------------------------------------
# Backend discovery / CLI
# --------------------------------------------------------------------------

def _make_transport(args) -> tuple:
    """Return (transport, framing) per CLI arguments."""
    if args.backend == "sim" or args.sim:
        try:
            import firmware_sim
        except ImportError:  # running from another cwd; use this file's dir
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import firmware_sim  # type: ignore
        dev = firmware_sim.FakeDevice(max_packet=args.sim_packet)
        return firmware_sim.SimTransport(dev), "usb-midi"
    needle = args.port or "XMOS"
    if args.backend == "rawmidi":
        # Kernel raw MIDI node. Works, but see RawMidiTransport: on this device
        # snd-usb-audio only delivers reliably right after a fresh bind.
        return RawMidiTransport(needle), "raw"
    if args.backend == "rtmidi":
        return RtMidiTransport(needle), "raw"
    if args.backend == "pyusb":
        pid = args.pid if args.pid is not None else 0x0018
        vid = args.vid if args.vid is not None else 0x20B1
        return PyUsbTransport(vid, pid), "usb-midi"
    # auto: raw bulk endpoints when we are allowed to take the interface off
    # the kernel (root), because that path round-trips reliably; then the
    # kernel raw MIDI node; then the ALSA sequencer.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            return PyUsbTransport(0x20B1, 0x0018), "usb-midi"
        except DiagError:
            pass
    if RawMidiTransport._find(needle):
        return RawMidiTransport(needle), "raw"
    try:
        import rtmidi
        if any(needle.lower() in p.lower() for p in rtmidi.MidiOut().get_ports()):
            return RtMidiTransport(needle), "raw"
    except ImportError:
        pass
    raise DiagError("no device found (try --list; or --backend pyusb --vid --pid)")


def _do_list() -> None:
    print("== rtmidi / ALSA ports ==")
    try:
        import rtmidi
        ports = sorted(set(rtmidi.MidiIn().get_ports()) |
                       set(rtmidi.MidiOut().get_ports()))
        print("\n".join(ports) if ports else "(none)")
    except ImportError:
        print("(python-rtmidi not installed)")
    print("== pyusb devices with USB-MIDI bulk endpoints ==")
    try:
        import usb.core
        found = False
        for dev in usb.core.find(find_all=True):
            try:
                cfg = _config_bytes(dev)
                ep_out, ep_in = find_midi_bulk_endpoints(cfg)
            except Exception:
                continue
            if ep_out is not None and ep_in is not None:
                found = True
                print(f"  {dev.idVendor:04x}:{dev.idProduct:04x} "
                      f"out=0x{ep_out:02x} in=0x{ep_in:02x}")
        if not found:
            print("(none)")
    except ImportError:
        print("(pyusb not installed)")


def _run_commands(client: CsdDiag, commands: Iterable[str]) -> int:
    rc = 0
    for cmd in commands:
        cmd = cmd.strip()
        if not cmd or cmd.startswith("#"):
            continue
        if "?" not in cmd:
            client.command(cmd)             # set-command: device stays silent
            continue
        try:
            print(client.query(cmd))
        except DeviceError as e:
            print(e.text, file=sys.stderr)
            rc = 1
        except DiagTimeout:
            # A rejected command is SILENT: the callback returns SCPI_RES_ERR
            # and emits no SysEx, so a timeout here means either "the device
            # refused this" or "the device is not there". Those read
            # identically and are not the same problem -- conflating them sent
            # two rounds of debugging into the USB-MIDI framing path while a
            # base64 decoder was rejecting the payload. The error queue
            # distinguishes them, and it stays readable after a timed-out read
            # (verified on hardware), so ask it before calling the transport
            # dead.
            reason = None
            try:
                q = client.query("SYSTem:ERRor:NEXT?", timeout=2,
                                 raise_error=False).strip()
                if q and not q.startswith("0,"):
                    reason = q
            except Exception:
                pass
            if reason:
                print(f"REJECTED {cmd!r}: {reason}", file=sys.stderr)
                rc = 1
            else:
                print(f"TIMEOUT: no reply and no queued error for {cmd!r}",
                      file=sys.stderr)
                rc = 2
    return rc


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="csd_diag.py",
        description="SCPI diagnostics over USB-MIDI for ClearSound UAC (XMOS XU316)")
    ap.add_argument("target", nargs="*",
                    help="SCPI command(s), or a single path to a file of commands")
    ap.add_argument("--list", action="store_true", help="list MIDI ports/devices")
    ap.add_argument("--repl", action="store_true", help="interactive session")
    ap.add_argument("--backend", choices=("auto", "rawmidi", "rtmidi", "pyusb", "sim"),
                    default="auto")
    ap.add_argument("--sim", action="store_true",
                    help="use the built-in fake device (acceptance/demo mode)")
    ap.add_argument("--sim-packet", type=int, default=USB_PACKET_SIZE,
                    help="fake device USB packet size (default 64)")
    ap.add_argument("--port", help="rtmidi port substring (default 'XMOS')")
    ap.add_argument("--vid", type=lambda s: int(s, 16), help="USB vendor id, hex")
    ap.add_argument("--pid", type=lambda s: int(s, 16), help="USB product id, hex")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = ap.parse_args(argv)

    if args.list:
        _do_list()
        return 0

    commands: List[str] = []
    if args.target:
        # A single existing path means "file of commands"; anything else is a
        # literal command list. Checked before treating the first token as a
        # file so `csd_diag.py "*IDN?" "DIAG:DSP:PRESet?"` works as expected
        # even if some file happens to share the name of the first command.
        if len(args.target) == 1 and os.path.isfile(args.target[0]):
            with open(args.target[0]) as f:
                commands = f.read().splitlines()
        else:
            commands = list(args.target)

    try:
        transport, framing = _make_transport(args)
    except DiagError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    client = CsdDiag(transport, framing=framing)
    try:
        if args.repl:
            print("csd_diag REPL - enter SCPI commands, Ctrl-D to exit")
            while True:
                try:
                    line = input("csd> ")
                except EOFError:
                    return 0
                if line.strip():
                    _run_commands(client, [line])
            return 0
        if not commands:
            ap.error("no command given (or use --repl / --list)")
        return _run_commands(client, commands)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
