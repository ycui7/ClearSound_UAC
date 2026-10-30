#!/usr/bin/env python3
"""In-process fake ClearSound CSD-AN22 diagnostic device, no real USB.

Approved command set (limited by the owner — anything else answers
-201,Undefined header, never a value):

    *IDN?
    SYSTem:ERRor[:NEXT]?            SYSTem:ERRor:COUNt?
    DIAG:DSP:PRESet                 DIAG:DSP:PRESet <0|1|2>
    REGister:READ? <hex>            REGister:WRITE <hex>,<hex>

Wire format (identical on both directions):

  host -> device: 4-byte USB-MIDI event packets carrying
                  F0 7E <ascii cmd> F7   (0x7E = MMA Educational & Research)
  device -> host: same shape for the SCPI reply (CR/LF stripped), split
                  into bulk packets of at most *max_packet* bytes.

Event byte0 = (cable<<4)|CIN — high nibble cable number, low nibble code
index number (USB Audio 1.0 Table 4-6; lib_xua midiinparse.xc makeEvent()
builds events as (cable_number << 28) | (codeIndexNumber << 24) | ..., and
usb_midi.xc byterev()s the little-endian word on the host->device path).

Every byte between F0 and F7 (exclusive) is a 7-bit MIDI data byte
(< 0x80) so the vendor device-side parser keeps the SysEx message.

HONESTY RULE: values are produced ONLY for BACKED commands (real device
state: the fake codec register map, the error queue, the DSP preset slot).
Anything the real device cannot do yet is STUBBED and returns the standard
SCPI error '-201,Undefined header' - never a made-up number.  The single
source of truth is BACKING below; tests assert the stubs really error.

Run this file directly for a smoke test.
"""

from __future__ import annotations

import base64
import re
import time
from collections import deque
from typing import Deque, List, Optional, Tuple

from csd_diag import (
    USB_PACKET_SIZE,
    SysexCollector,
    UsbMidiEventParser,
    build_sysex,
    pack_packets,
    parse_sysex,
)

# ---------------------------------------------------------------------------
# Backed vs stubbed command map (HONESTY RULE - tests assert on this table).
#   "backed" - device state genuinely backs the answer
#   "stub"   - recognised family, NOT implementable on the real device yet;
#              returns SCPI error -201,Undefined header
# Owner-approved command set only: MEASure:*, SOURce:*, CONFigure:* are
# STUBBED (they must answer -201, never a value).
# ---------------------------------------------------------------------------

BACKING: List[Tuple[str, str, str]] = [
    ("*IDN?",                    "backed", "vendor/model/serial/fw identity"),
    ("*OPC?",                    "backed", "immediate 1 (no long ops)"),
    ("SYSTem:ERRor[:NEXT]?",     "backed", "pop next error queue entry"),
    ("SYSTem:ERRor:COUNt?",      "backed", "error queue depth"),
    ("DIAG:DSP:PRESet[<0|1|2>]", "backed", "DSP preset slot 0/1/2 in state"),
    ("REGister:READ? \"<b64 [addr,...]>\"",  "backed", "fake codec register map read"),
    ("REGister:WRITE \"<b64 [addr,val,...]>\"", "backed", "fake codec register map write"),
    ("MEASure:*",                "stub",   "outside approved command set"),
    ("SOURce:*",                 "stub",   "outside approved command set"),
    ("CONFigure:*",              "stub",   "outside approved command set"),
    ("(anything else)",          "stub",   "-201,Undefined header"),
]

# Sample commands exercising each stub row (used by the honesty tests).
STUB_SAMPLES = {
    "MEASure:*": "MEASure:VAC:RMS? CH1",
    "SOURce:*": "SOURce:VAC:AMPLitude 1.5",
    "CONFigure:*": "CONFigure:VAC 10.0",
    "(anything else)": "BOGUS:THING? 42",
}

DEFAULT_RATE = 384000.0        # device sample rate (Hz, informational)

# Fake codec register map: addr -> value.  Unknown addresses error.
# Values are 8-bit because the ES9219Q register file is 8-bit wide -- the
# device truncates on both read and write (csd_diag_scpi.c stores
# (unsigned char)val), so a 16-bit fake here would let the client assert on
# values the hardware can never return. The old 0xC5D1 chip-id fake was 16-bit
# and the pair (write 0x2A, read 0x002A) passed while the device would answer
# 0x2A.
#
# The address space is the FULL 8-bit range and every address reads, because
# that is what the device does: csd_diag_regs.xc rejects only reg > 0xFF and
# otherwise returns whatever the audiohw server answers over I2C -- there is no
# per-address "implemented" table and no 0x3F wall. The earlier fake invented
# both, so tests passed against a device that could never reject those
# commands and failed against one that never would. Unknown addresses now
# read back 0, which is what an unpopulated codec register reads as.
REG_ADDR_MIN, REG_ADDR_MAX = 0x00, 0xFF
CSD_REG_MAX = 64             # device's CSD_REG_MAX: payload cap, both ways
REG_CHIP_ID = 0x5A           # codec chip id at address 0x00 (8-bit, as read)
REG_VOL_DEFAULT = 0x30       # default volume at address 0x03
REGISTERS = {0x00: REG_CHIP_ID, 0x03: REG_VOL_DEFAULT}


class FakeDevice:
    """Byte-pipe fake diagnostic device with USB-MIDI framing on both directions.

    *framing* "usb-midi": queues carry 4-byte event packets (bulk pipe).
    *framing* "raw":      queues carry raw MIDI byte streams (ALSA path).
    """

    def __init__(self, max_packet: int = USB_PACKET_SIZE, framing: str = "usb-midi",
                 serial: str = "SN0001", firmware: str = "1.0.0-sim"):
        if framing not in ("usb-midi", "raw"):
            raise ValueError(f"unknown framing {framing!r}")
        self.max_packet = max_packet
        self.framing = framing
        self.model = "CSD-AN22"
        self.serial = serial
        self.firmware = firmware
        self.rate = DEFAULT_RATE
        self.dsp_preset = 0                  # DIAG:DSP:PRESet slot
        self.regs = dict(REGISTERS)          # fake codec register map
        self.error_queue: Deque[str] = deque()
        self.rx: Deque[bytes] = deque()   # host -> device bytes
        self.tx: Deque[bytes] = deque()   # device -> host bytes
        self._in_events = UsbMidiEventParser()
        self._in_raw = SysexCollector()
        self.commands_seen: List[str] = []

    # -- byte pipes ------------------------------------------------------

    def write_from_host(self, data: bytes) -> None:
        """Host writes bulk-OUT bytes."""
        self.rx.append(bytes(data))

    def read_by_host(self, size: int) -> bytes:
        """Host reads up to *size* bytes of bulk-IN data (b'' if empty)."""
        out = bytearray()
        while self.tx and len(out) < size:
            chunk = self.tx.popleft()
            if len(out) + len(chunk) > size:
                self.tx.appendleft(chunk[size - len(out):])
                chunk = chunk[:size - len(out)]
            out.extend(chunk)
        return bytes(out)

    def _emit_reply(self, text: str) -> None:
        sysex = build_sysex(text)
        if self.framing == "usb-midi":
            for pkt in pack_packets(sysex, self.max_packet):
                self.tx.append(pkt)
        else:
            self.tx.append(sysex)

    # -- command processing ----------------------------------------------

    def process(self) -> int:
        """Drain pending host bytes, answer every complete SysEx.

        Returns the number of commands handled.
        """
        messages: List[bytes] = []
        while self.rx:
            chunk = self.rx.popleft()
            messages.extend(
                self._in_events.feed(chunk) if self.framing == "usb-midi"
                else self._in_raw.feed(chunk))
        handled = 0
        for msg in messages:
            try:
                cmd = parse_sysex(msg)
            except Exception:
                continue                    # ignore foreign SysEx
            self.commands_seen.append(cmd)
            reply = self._handle(cmd)
            if reply is not None:
                self._emit_reply(reply)
            handled += 1
        return handled

    # -- helpers ----------------------------------------------------------

    def _push_error(self, code: int, msg: str) -> None:
        self.error_queue.append(f"-{code},{msg}")

    def _undefined(self, cmd: str):
        """Unrecognised header: queue the error and answer NOTHING.

        Two corrections, both forced by hardware rather than by taste:

        * The code is -113 (SCPI_ERROR_UNDEFINED_HEADER in libscpi's
          inc/scpi/error.h). -201 was invented; -201 is
          SCPI_ERROR_INVALID_WHILE_IN_LOCAL. Confirmed on the bench: the
          device answers SYSTem:ERRor:NEXT? with '-113,"Undefined header"'.
        * There is NO reply. An unknown header fails header matching, the
          command never runs, and a callback that does not emit produces no
          SysEx at all -- so the host must poll the error queue. The sim used
          to return the error text as if it were a reply, which taught every
          test written against it to await a reply for a command that can
          never send one. That is the same class of divergence as the
          quoted-vs-raw payload bug: a sim that is wrong in a way the tests
          then assert is worse than no sim.
        """
        self._push_error(113, f'Undefined header, "{cmd}"')
        return None

    @staticmethod
    def _parse_hex_arg(tok: str) -> Optional[int]:
        """Parse a hex register argument ('0x03', '3f', '0'); None if bad.

        Value range is checked by the caller (address vs data ranges differ).

        KEPT, and used by no register command any more. The register path
        moved to base64 payloads (see _b64_arg and the REGister handlers)
        because libscpi's lexer rejects hex spellings like 0x03/0X03 outright,
        measured on the real parser via native_scpi_harness.c. This helper
        stays because the client's reg_read/reg_write validate their own
        inputs in Python and a future hex-formatted diagnostic command should
        reach for the same parser rather than invent one. It is NOT evidence
        that hex arguments work over the wire -- they do not.
        """
        m = re.fullmatch(r"(?i)(0x)?([0-9a-f]+)", tok.strip())
        if not m:
            return None
        return int(m.group(2), 16)

    @staticmethod
    def _b64_arg(tok: str) -> Optional[bytes]:
        """Decode a base64 register payload; None on any malformed input.

        Mirrors the device's strict decoder in csd_diag_scpi.c (b64_decode):
        canonical alphabet, padding required, no whitespace. Rejection rather
        than truncation, and the sim must be as strict as the firmware -- a
        permissive simulator accepts commands the real device refuses and
        turns a bug into a green test run.
        """
        if len(tok) == 0 or len(tok) % 4:
            return None
        try:
            return base64.b64decode(tok, validate=True)
        except Exception:
            return None

    # -- command dispatch ---------------------------------------------------

    def _handle(self, cmd: str) -> Optional[str]:
        """Return the SCPI reply text, or None for set-commands (no reply).

        Only the owner-approved command set is backed; everything else
        (including MEASure:*, SOURce:*, CONFigure:*) answers -201,
        Undefined header — never a value.
        """
        c = cmd.strip()
        up = c.upper()

        if up == "*IDN?":
            return f"ClearSound,{self.model},{self.serial},{self.firmware}"
        if up == "*OPC?":
            return "1"
        if up in ("SYSTEM:ERROR?", "SYSTEM:ERROR:NEXT?"):
            if self.error_queue:
                return self.error_queue.popleft()
            return "0,No error"
        if up == "SYSTEM:ERROR:COUNT?":
            return str(len(self.error_queue))

        m = re.fullmatch(r"(?i)DIAG:DSP:PRESet\?*(?:\s+(\S+))?", c)
        if m:
            if m.group(1) is None:                 # query form -> current slot
                return str(self.dsp_preset)
            if m.group(1) not in ("0", "1", "2"):
                # -224 Illegal parameter value: mirrors set_dsp() in
                # csd_diag_scpi.c, which pushes SCPI_ERROR_ILLEGAL_PARAMETER_VALUE
                # for v > 2 (libscpi error.h: -224, not -222 "Data out of
                # range" and not -104 "Data type error").
                self._push_error(224, "Illegal parameter value")
                return None                        # silent; poll SYST:ERR
            self.dsp_preset = int(m.group(1))
            return None                            # set form is silent

        # ---- registers: base64 payload lists (must match csd_diag_scpi.c) --
        #
        # The wire form is the BATMCU convention, mirrored on the device side
        # in csd_diag_scpi.c (see b64_decode there for the measured reason:
        # libscpi's lexer rejects hex spellings like 0x03/0X03 outright).
        #   REGister:READ?  "<b64 [addr,...]>"  ->  "<b64 [val,...]>"
        #   REGister:WRITE  "<b64 [addr,val,...]>"   silent
        # This sim MUST implement the same contract the firmware does. When it
        # carried the old '<hex>'/'<hex>,<hex>' syntax it passed 52/52 while
        # the real device rejected every parameterised command -- a simulator
        # that agrees only with itself is worse than no simulator, because it
        # converts an unknown into a false PASS.
        m = re.fullmatch(r"(?i)REGister:READ\?\s+\"([A-Za-z0-9+/=]*)\"", c)
        if m:
            # Mirrors csd_diag_scpi.c reg_read_q: a bad payload, an empty one,
            # or one over CSD_REG_MAX all push SCPI_ERROR_INVALID_STRING_DATA
            # (-222), because b64_decode returns <=0 in all three cases (it
            # refuses rather than truncating at the cap). No other error is
            # reachable: csd_diag_reg_read only fails on reg > 0xFF and an
            # address arrives as one byte, so that is unreachable by
            # construction -- the old 0x3F wall and "Register not implemented"
            # errors here were inventions, and tests written against them
            # failed on real hardware that never rejects those commands.
            addrs = self._b64_arg(m.group(1))
            if not addrs or len(addrs) > CSD_REG_MAX:
                # Silent, like the device: reg_read_q pushes
                # SCPI_ERROR_INVALID_STRING_DATA and returns SCPI_RES_ERR,
                # which emits no reply. Awaiting a reply here is the trap
                # that read as a dead transport for two debugging rounds.
                self._push_error(151, "Invalid string data")
                return None
            vals = bytes((self.regs.get(a, 0) & 0xFF) for a in addrs)
            # RAW base64, not quoted: the device emits it via
            # SCPI_ResultCharacters (csd_diag_scpi.c), which writes the
            # characters with no quotes. Verified on hardware, where the reply
            # to a 2-address read was the 4 bytes AEA=. The sim used to wrap
            # the payload in quotes, so the client -- written against the sim
            # -- required quotes and failed every real read.
            return base64.b64encode(vals).decode()

        m = re.fullmatch(r"(?i)REGister:WRITE\s+\"([A-Za-z0-9+/=]*)\"", c)
        if m:
            buf = self._b64_arg(m.group(1))
            # Same -222 for all three rejections, per csd_diag_scpi.c reg_write:
            # undecodable, empty, odd length, or over the cap. A malformed
            # payload decodes to nothing, so nothing is written -- that is the
            # whole of the device's guarantee. It does NOT pre-validate the
            # list and then apply it: writes go to the codec in order, and an
            # I2C failure partway leaves the earlier registers written (the
            # ES9219Q has no dry-run). A fake that applied all-or-nothing
            # would let a harness assume a failed batch rolled back. It did not.
            if buf is None or not buf or len(buf) % 2 or len(buf) > 2 * CSD_REG_MAX:
                self._push_error(151, "Invalid string data")
                return None                        # silent; poll SYST:ERR
            for i in range(0, len(buf), 2):
                self.regs[buf[i]] = buf[i + 1]     # in order, as the device does
            return None                            # set-command: silent

        # MEASure:*, SOURce:*, CONFigure:*, *RST/*CLS, any unrecognised
        # header: honest -201, never a fake value.
        return self._undefined(c)


class SimTransport:
    """Transport-protocol adapter onto a FakeDevice (in-process pipes)."""

    def __init__(self, device: FakeDevice, pump: bool = True):
        self._dev = device
        self._pump = pump

    def send(self, data: bytes) -> None:
        self._dev.write_from_host(data)
        if self._pump:
            self._dev.process()

    def recv(self, size: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            data = self._dev.read_by_host(size)
            if data:
                return data
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.001)

    def close(self) -> None:
        pass


def _smoke() -> None:
    import csd_diag
    dev = FakeDevice()
    client = csd_diag.CsdDiag(SimTransport(dev))
    print(client.query("*IDN?"))
    print(client.query("REGister:READ? 0x00"))
    client.command("REGister:WRITE 0x03,0x2A")
    print(client.query("REGister:READ? 0x03"))
    client.command("DIAG:DSP:PRESet 2")
    print(client.query("DIAG:DSP:PRESet?"))
    print(client.query("MEASure:VAC:RMS? CH1", raise_error=False))


if __name__ == "__main__":
    _smoke()
