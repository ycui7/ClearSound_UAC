#!/usr/bin/env python3
"""Unit tests for csd_diag + firmware_sim (the CSD-AN22 analyzer fake).

Run:  python3 -m unittest -v test_csd_diag.py
"""

from __future__ import annotations

import base64
import time
import unittest

import csd_diag
import firmware_sim
from csd_diag import (
    DiagError,
    DeviceError,
    DiagTimeout,
    SysexCollector,
    UsbMidiEventParser,
    build_sysex,
    find_midi_bulk_endpoints,
    pack_events,
    pack_packets,
    parse_sysex,
)
from firmware_sim import (
    BACKING,
    REG_CHIP_ID,
    REG_VOL_DEFAULT,
    STUB_SAMPLES,
    FakeDevice,
    SimTransport,
)


class WireFormatTests(unittest.TestCase):
    def test_build_sysex_frame(self):
        # Wire format: F0 7E <ascii> F7 (single 7-bit-safe ID byte 0x7E).
        self.assertEqual(
            build_sysex("*IDN?"),
            bytes([0xF0, 0x7E]) + b"*IDN?" + bytes([0xF7]))

    def test_all_payload_bytes_are_7bit(self):
        # HARD firmware constraint: every byte between F0 and F7 (exclusive)
        # must be < 0x80 or the vendor device parser (midi_in_parse) drops
        # the message as a status byte.
        printable = "".join(chr(i) for i in range(0x20, 0x7F))
        for cmd in ("*IDN?", "MEASure:VAC:RMS? CH1",
                    "SOURce:VAC:AMPLitude 1.5", "CONFigure:VAC " + printable):
            msg = build_sysex(cmd)
            self.assertEqual(msg[0], 0xF0)
            self.assertEqual(msg[-1], 0xF7)
            for b in msg[1:-1]:
                self.assertLess(b, 0x80, f"non-7-bit byte {b:02x} in {cmd!r}")

    def test_non_ascii_command_rejected_client_side(self):
        for bad in ("MEASure:VAC:RMS? \u00c9", "CMD \x80", "\u4e2d\u6587"):
            with self.assertRaises(csd_diag.DiagError):
                build_sysex(bad)
        # and it never reaches the wire through the client
        dev = FakeDevice()
        client = csd_diag.CsdDiag(SimTransport(dev))
        with self.assertRaises(csd_diag.DiagError):
            client.query("MEASure:VAC:RMS? \u00c9")
        self.assertEqual(dev.commands_seen, [])     # nothing sent
        client.close()

    def test_build_sysex_strips_crlf(self):
        self.assertEqual(build_sysex("*IDN?\r\n"), build_sysex("*IDN?"))

    def test_build_sysex_rejects_empty(self):
        with self.assertRaises(csd_diag.DiagError):
            build_sysex("\r\n")

    def test_parse_sysex_roundtrip(self):
        self.assertEqual(parse_sysex(build_sysex("*IDN?")), "*IDN?")

    def test_parse_sysex_rejects_bad_manufacturer(self):
        bad = bytes([0xF0, 0x7D]) + b"X" + bytes([0xF7])
        with self.assertRaises(csd_diag.DiagError):
            parse_sysex(bad)

    def test_pack_events_cin_schedule(self):
        # Wire header: byte0 = (cable<<4)|CIN — HIGH nibble cable, LOW nibble
        # CIN (USB Audio 1.0 Table 4-6; lib_xua midiinparse.xc makeEvent():
        # event = (cable_number << 28) | (codeIndexNumber << 24) | ... with
        # usb_midi.xc host->device `int event = byterev(datum)`).
        # len(sysex) = 3 + len(cmd); chunking starts at F0; final spec CIN:
        # 0x5 = ends with 2 data bytes, 0x6 = ends with 1, 0x7 = ends with 3.
        cases = {  # cmd_len -> (n_events, final CIN)
            1: (2, 0x6),      # total 4 -> 3+1  (spec: 1 byte -> 0x6)
            2: (2, 0x5),      # total 5 -> 3+2  (spec: 2 bytes -> 0x5)
            3: (2, 0x7),      # total 6 -> 3+3
            4: (3, 0x6),      # total 7 -> 3+3+1
        }
        for cmd_len, (n_ev, final_cin) in cases.items():
            sysex = build_sysex("A" * cmd_len)
            evs = pack_events(sysex)
            self.assertEqual(len(evs), n_ev, f"cmd_len={cmd_len}")
            for e in evs[:-1]:
                self.assertEqual(e[0], 0x04)              # CIN4, cable 0
            self.assertEqual(evs[-1][0], final_cin)       # cable 0, spec CIN
            rebuilt = b"".join(e[1:] for e in evs)[:len(sysex)]
            self.assertEqual(rebuilt, sysex)
            pad = 3 - {0x5: 2, 0x6: 1}.get(final_cin, 3)
            if pad:
                self.assertEqual(evs[-1][4 - pad:], bytes(pad))

    def test_pack_events_literal_wire_bytes_idn(self):
        # EXPLICIT WIRE VECTOR (the whole point of the nibble fix):
        # byte0 = (cable<<4)|CIN per USB Audio 1.0 Table 4-6 —
        # lib_xua/src/midi/midiinparse.xc makeEvent():
        #   event = (cable_number << 28) | (codeIndexNumber << 24) | ...
        # lib_xua/src/midi/usb_midi.xc (host->device): int event = byterev(datum)
        # "*IDN?" -> F0 7E 2A 49 44 4E 3F F7 : 3-byte chunks carry CIN 4 in
        # the LOW nibble (byte0 == 0x04: cable 0); the 2-byte tail carries the
        # spec end-CIN 0x5 (ends with 2 data bytes) in the low nibble.
        evs = pack_events(build_sysex("*IDN?"))
        self.assertEqual(evs, [
            bytes([0x04, 0xF0, 0x7E, 0x2A]),
            bytes([0x04, 0x49, 0x44, 0x4E]),
            bytes([0x05, 0x3F, 0xF7, 0x00]),
        ])
        for e in evs[:-1]:
            self.assertEqual(e[0], 0x04)          # cable 0, CIN 4
        self.assertEqual(evs[-1][0], 0x05)        # cable 0, spec end-CIN

    def test_pack_events_literal_wire_bytes_one_byte_tail(self):
        # 1-byte tail: spec CIN is 0x6 (ends with 1 data byte), byte0 = 0x06.
        evs = pack_events(build_sysex("A"))
        self.assertEqual(evs, [
            bytes([0x04, 0xF0, 0x7E, 0x41]),
            bytes([0x06, 0xF7, 0x00, 0x00]),
        ])

    def test_pack_packets_respects_packet_size(self):
        sysex = build_sysex("SOURce:VAC:AMPLitude " + "1" * 200)
        pkts = pack_packets(sysex, 64)
        self.assertTrue(all(len(p) % 4 == 0 and len(p) <= 64 for p in pkts))
        self.assertEqual(b"".join(pkts), b"".join(pack_events(sysex)))


class EventParserTests(unittest.TestCase):
    def _drive(self, chunks):
        p = UsbMidiEventParser()
        out = []
        for c in chunks:
            out.extend(p.feed(c))
        return out

    def test_reassembly_multi_packet(self):
        sysex = build_sysex("SYSTem:ERRor? " + "y" * 100)
        pkts = pack_packets(sysex, 16)              # force many small packets
        self.assertGreater(len(pkts), 4)
        msgs = self._drive(pkts)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0], sysex)

    def test_events_split_across_reads(self):
        stream = b"".join(pack_events(build_sysex("*IDN?")))
        chunks = [stream[i:i + 5] for i in range(0, len(stream), 5)]
        msgs = self._drive(chunks)                  # 5-byte reads split events
        self.assertEqual([parse_sysex(m) for m in msgs], ["*IDN?"])

    def test_two_messages_back_to_back(self):
        a = b"".join(pack_events(build_sysex("*OPC?")))
        b = b"".join(pack_events(build_sysex("*IDN?")))
        msgs = self._drive([a + b[:7], b[7:]])
        self.assertEqual([parse_sysex(m) for m in msgs], ["*OPC?", "*IDN?"])

    def test_parse_hand_written_spec_packet(self):
        # Parser accepts what a SPEC-conformant vendor device sends:
        # byte0 = 0x04 for CIN-4 events (cable 0, CIN 4 in LOW nibble),
        # spec final CINs 0x5 (2 bytes) / 0x6 (1 byte) / 0x7 (3 bytes).
        # Stream carries "*IDN?" as reply: F0 7E 2A 49 44 4E 3F F7.
        evs = [bytes([0x04, 0xF0, 0x7E, 0x2A]),
               bytes([0x04, 0x49, 0x44, 0x4E]),
               bytes([0x05, 0x3F, 0xF7, 0x00])]
        msgs = self._drive([b"".join(evs)])
        self.assertEqual([parse_sysex(m) for m in msgs], ["*IDN?"])

    def test_parse_vendor_convention_packet(self):
        # The firmware reply producer (lib_xua midiinparse.xc) emits
        # codeIndexNumber = received + 0x4, i.e. 0x4+k for a k-byte tail —
        # spec-disagreeing for k=1/2.  The receiver must locate F7 in
        # bytes 1..3 and accept ANY final CIN 0x5/0x6/0x7.
        # Same reply as above but the 2-byte tail [3F F7] arrives with the
        # VENDOR byte0 0x06 (0x4+2) instead of the spec 0x05:
        evs = [bytes([0x04, 0xF0, 0x7E, 0x2A]),
               bytes([0x04, 0x49, 0x44, 0x4E]),
               bytes([0x06, 0x3F, 0xF7, 0x00])]
        msgs = self._drive([b"".join(evs)])
        self.assertEqual([parse_sysex(m) for m in msgs], ["*IDN?"])
        # and the 1-byte tail [F7] arriving with vendor byte0 0x05 (0x4+1)
        # instead of the spec 0x06:
        evs = [bytes([0x04, 0xF0, 0x7E, 0x41]),
               bytes([0x05, 0xF7, 0x00, 0x00])]
        msgs = self._drive([b"".join(evs)])
        self.assertEqual([parse_sysex(m) for m in msgs], ["A"])
        # a final event whose F7 sits at byte 3 (CIN 0x7 shape) under a
        # wrong-but-tolerated CIN 0x5:
        evs = [bytes([0x04, 0xF0, 0x7E, 0x41]),
               bytes([0x05, 0x42, 0x43, 0xF7])]
        msgs = self._drive([b"".join(evs)])
        self.assertEqual([parse_sysex(m) for m in msgs], ["ABC"])

    def test_lenient_cin5_two_byte_variant(self):
        # Final event CIN 0x5 carrying [data, F7] (vendor byte0 = 0x05 for a
        # 2-byte tail).  Stream: F0 7E 'O' | 'K' F7 -> message "OK".
        evs = [bytes([0x04, 0xF0, 0x7E, ord('O')]),
               bytes([0x05, ord('K'), 0xF7, 0x00])]
        msgs = self._drive([b"".join(evs)])
        self.assertEqual([parse_sysex(m) for m in msgs], ["OK"])

    def test_non_sysex_events_ignored(self):
        note_on = bytes([0x09, 0x3C, 0x7F, 0x00])   # CIN 0x9 not SysEx -> dropped
        msgs = self._drive([note_on +
                            b"".join(pack_events(build_sysex("A")))])
        self.assertEqual([parse_sysex(m) for m in msgs], ["A"])
        # an event with a non-zero cable number is dropped too
        wrong_cable = bytes([0x14, 0xF0, 0x7E, 0x41])  # cable 1, CIN 4
        msgs = self._drive([wrong_cable +
                            b"".join(pack_events(build_sysex("A")))])
        self.assertEqual([parse_sysex(m) for m in msgs], ["A"])


class RawCollectorTests(unittest.TestCase):
    def test_raw_sysex_and_realtime_interleave(self):
        c = SysexCollector()
        sysex = build_sysex("OK")
        data = sysex[:-1] + bytes([0xF8]) + sysex[-1:]   # clock interleaved
        self.assertEqual(c.feed(data), [sysex])

    def test_payload_bytes_ge_0x80_kept(self):
        # 0x7E ID is 7-bit, but exercise tolerance generally: byte 0x7F kept.
        c = SysexCollector()
        msg = bytes([0xF0, 0x7E, 0x7F, 0xF7])
        self.assertEqual(c.feed(msg), [msg])

    def test_raw_split_feed(self):
        c = SysexCollector()
        sysex = build_sysex("*IDN?")
        out = c.feed(sysex[:3]) + c.feed(sysex[3:])
        self.assertEqual(out, [sysex])


class _QueueAssertions:
    """Error-queue polling, shared by every class that talks to the sim.

    A rejected command is SILENT on this device -- the callback returns
    SCPI_RES_ERR and emits no SysEx -- so the only way to observe a rejection
    is to read SYSTem:ERRor:COUNt? / SYSTem:ERRor? afterwards. That is exactly
    what hw_acceptance.py does on the bench; tests must use the same contract
    the hardware forces, or they assert a reply that can never arrive.
    """

    def _queued_error(self, cmd):
        self.client.query("SYSTem:ERRor:NEXT?", raise_error=False)   # drain
        self.client.command(cmd)
        depth = self.client.query("SYSTem:ERRor:COUNt?", raise_error=False)
        text = self.client.query("SYSTem:ERRor?", raise_error=False)
        return depth, text


class AnalyzerBackedTests(unittest.TestCase, _QueueAssertions):
    """Backed commands: genuine device state, coherent values."""

    def setUp(self):
        self.dev = FakeDevice()
        self.client = csd_diag.CsdDiag(SimTransport(self.dev))

    def tearDown(self):
        self.client.close()

    def _silent(self, cmd):
        """Send a set-command (no reply expected)."""
        self.client.command(cmd)

    def test_idn(self):
        self.assertEqual(self.client.query("*IDN?"),
                         "ClearSound,CSD-AN22,SN0001,1.0.0-sim")

    def test_opc(self):
        self.assertEqual(self.client.query("*OPC?"), "1")

    def test_register_read_defaults(self):
        # base64 payload lists: addresses in, values out, positionally aligned
        self.assertEqual(self.client.reg_read(0x00), bytes([REG_CHIP_ID]))
        self.assertEqual(self.client.reg_read(0x03), bytes([REG_VOL_DEFAULT]))
        self.assertEqual(self.client.reg_read([0x00, 0x03]),
                         bytes([REG_CHIP_ID, REG_VOL_DEFAULT]))

    def test_register_read_is_one_round_trip_for_a_range(self):
        # 0x01/0x02 are not in the fake map, so put values there first: a
        # contiguous range must come back from ONE command, not one per byte.
        self.client.reg_write({0x01: 0x11, 0x02: 0x22})
        before = len(self.dev.commands_seen)
        m = self.client.reg_read_map(0x00, 0x03)
        self.assertEqual(len(self.dev.commands_seen) - before, 1,
                         "a register range must cost one command, not one per byte")
        self.assertEqual(m, {0x00: REG_CHIP_ID, 0x01: 0x11, 0x02: 0x22,
                             0x03: REG_VOL_DEFAULT})

    def test_register_write_and_readback(self):
        self.client.reg_write({0x03: 0x2A})
        self.assertEqual(self.client.reg_read(0x03), bytes([0x2A]))
        self.assertEqual(self.dev.regs[0x03], 0x2A)

    def test_register_wire_form_is_quoted_base64(self):
        # The command on the wire, asserted literally rather than through the
        # helpers: the quoting and the padding are what libscpi's lexer accepts,
        # and a helper that silently built the old unquoted hex form would keep
        # passing every other test here while the device rejected every command.
        self.client.reg_read([0x03])
        self.assertEqual(self.dev.commands_seen[-1], 'REGister:READ? "Aw=="')
        self.client.reg_write({0x03: 0x2A})
        self.assertEqual(self.dev.commands_seen[-1], 'REGister:WRITE "Ayo="')

    def test_register_write_list_applies_all_pairs(self):
        self.client.reg_write([(0x03, 0x11), (0x00, 0x22)])
        self.assertEqual(self.client.reg_read([0x00, 0x03]), bytes([0x22, 0x11]))

    def test_register_replies_are_8bit(self):
        # The ES9219Q register file is 8-bit wide. Asserting a 16-bit reply
        # here (the old "0x002A") passed against the fake and could never be
        # true of the hardware.
        self.client.reg_write({0x03: 0xFF})
        self.assertEqual(self.client.reg_read(0x03), bytes([0xFF]))
        self.assertLessEqual(self.client.reg_read(0x03)[0], 0xFF)

    def test_register_reads_never_reject_an_in_range_address(self):
        # The device answers ANY byte address: csd_diag_regs.xc rejects only
        # reg > 0xFF (unreachable over a byte-typed payload) and the I2C server
        # returns whatever the codec line reads -- an unpopulated register
        # reads 0, it does not error. Asserting an error for an unmapped
        # address was testing a fake, not the device.
        self.assertEqual(self.client.reg_read(0x21), bytes([0x00]))
        self.assertEqual(self.client.reg_read(0xFF), bytes([0x00]))

    def test_register_read_rejects_short_reply(self):
        # The device is all-or-nothing, so a short dump can only come from a
        # transport glitch. Simulate the glitch at the query layer: the client
        # must raise, never pad -- a silently short dump decodes as a
        # plausible map of registers we never actually read.
        good = self.client.query
        self.client.query = lambda *a, **k: '"AA=="'   # 1 byte for a 3-addr ask
        try:
            with self.assertRaises(DiagError):
                self.client.reg_read([0x00, 0x01, 0x02])
        finally:
            self.client.query = good

    def test_register_read_accepts_bare_reply(self):
        # The device emits the payload with no quotes (SCPI_ResultCharacters),
        # measured on hardware as the 4 bytes "AEA=" for a 2-address read. This
        # asserts the accepted form FIRST so a future "require quotes" change
        # breaks here instead of on the bench -- which is how this bug hid: the
        # sim quoted, the client required quotes, the device did neither.
        good = self.client.query
        self.client.query = lambda *a, **k: "MA=="
        try:
            self.assertEqual(self.client.reg_read(0x03), bytes([0x30]))
        finally:
            self.client.query = good

    def test_register_read_still_accepts_quoted_reply(self):
        # A quoted payload from a SCPI_ResultQuotedCharacters build must decode
        # too, so the client does not pin us to one C++ emission variant.
        good = self.client.query
        self.client.query = lambda *a, **k: '"MA=="'
        try:
            self.assertEqual(self.client.reg_read(0x03), bytes([0x30]))
        finally:
            self.client.query = good

    def test_register_read_rejects_error_reply_as_payload(self):
        # An error string must never decode as data. "-151,Invalid string data"
        # contains characters outside the base64 alphabet, so validation
        # rejects it; if it ever decoded, a failed read would look like a
        # plausible register map.
        good = self.client.query
        self.client.query = lambda *a, **k: "-151,Invalid string data"
        try:
            with self.assertRaises(DiagError):
                self.client.reg_read(0x03)
        finally:
            self.client.query = good

    def test_register_read_rejects_empty_reply(self):
        good = self.client.query
        self.client.query = lambda *a, **k: ""
        try:
            with self.assertRaises(DiagError):
                self.client.reg_read(0x03)
        finally:
            self.client.query = good

    def test_register_read_client_rejects_out_of_range_before_wire(self):
        # 0x100 cannot be a byte; the client must refuse locally and never put
        # a command on the wire that the device's byte payload cannot express.
        with self.assertRaises(DiagError):
            self.client.reg_read(0x100)
        with self.assertRaises(DiagError):
            self.client.reg_read([0x00, 0x100])

    def test_register_read_rejects_oversized_payload(self):
        # Device cap is CSD_REG_MAX (64): b64_decode refuses rather than
        # truncating, so a 65-address dump is rejected with nothing emitted --
        # silently, with the reason in the error queue.
        depth, text = self._queued_error('REGister:READ? "%s"' %
                                         base64.b64encode(bytes(65)).decode())
        self.assertEqual(depth, "1")
        self.assertEqual(text, "-151,Invalid string data")

    def test_register_read_bad_base64_errors(self):
        # unpadded: strict on both sides, and SILENT. Awaiting a reply here is
        # the trap that read as a dead transport for two debugging rounds.
        depth, text = self._queued_error('REGister:READ? "Aw"')
        self.assertEqual(depth, "1")
        self.assertEqual(text, "-151,Invalid string data")

    def test_register_read_bad_base64_produces_no_reply(self):
        # The rejection must be silent, not a reply carrying the error text:
        # a sim that answered '-151,...' would let a test pass that hangs on
        # hardware.
        self.client.query("SYSTem:ERRor:NEXT?", raise_error=False)
        with self.assertRaises(DiagTimeout):
            self.client.query('REGister:READ? "Aw"', timeout=0.3)

    def test_register_read_old_hex_form_is_rejected(self):
        # Regression guard for the whole episode: the pre-base64 syntax must
        # NOT be accepted. It was the syntax the real parser rejected, and a
        # sim that kept answering it is what let 52/52 pass over a dead device.
        depth, text = self._queued_error("REGister:READ? 0x03")
        self.assertEqual(depth, "1")
        # libscpi's lexer rejects this token shape before any callback runs
        # (measured on the native harness: -101 Invalid character), or it
        # reaches the decoder and -151s. Either way: rejected, silent, queued.
        # On the bench the real lexer answers -101 Invalid character for this
        # shape; the sim has no lexer, it pattern-matches, so it rejects as an
        # unknown header. Both are rejections -- assert the invariant that
        # matters (never answered with data) and name the difference rather
        # than pinning one code the sim cannot produce.
        self.assertTrue(text.startswith(("-101,", "-151,", "-113,")),
                        "old hex form must be rejected, got %r" % text)

    def test_register_write_bad_payload_silent_but_queued(self):
        self._silent('REGister:WRITE "Ayo="')           # [0x03,0x2A] ok pairs
        self.assertEqual(self.client.query("SYSTem:ERRor:COUNt?"), "0")
        self._silent('REGister:WRITE "Ayov"')           # 3 bytes: not pairs
        self.assertEqual(self.client.query("SYSTem:ERRor:COUNt?"), "1")
        # -151: the malformation is in the string, not a numeric value, so the
        # device pushes INVALID_STRING_DATA (csd_diag_scpi.c reg_write).
        self.assertEqual(self.client.query("SYSTem:ERRor?", raise_error=False),
                         "-151,Invalid string data")

    def test_register_write_accepts_whole_address_space(self):
        # 0x40 and 0xFF are ordinary codec addresses. The fake used to reject
        # anything above 0x3F and the tests asserted that; the device never
        # does (csd_diag_regs.xc caps at reg > 0xFF only).
        self.client.reg_write({0x40: 0x01, 0xFF: 0xFE})
        self.assertEqual(self.client.query("SYSTem:ERRor:COUNt?"), "0")
        self.assertEqual(self.client.reg_read([0x40, 0xFF]), bytes([0x01, 0xFE]))

    def test_register_writes_do_not_roll_back(self):
        # NOT all-or-nothing. The device has no pre-validation pass and no
        # dry-run: reg_write walks the decoded list writing as it goes, so a
        # good batch followed by a rejected one leaves the good registers
        # WRITTEN. A test that asserted rollback here would have encoded a
        # safety property the hardware does not offer, and a harness relying on
        # it would believe a half-written codec was untouched. The only real
        # guarantee is that a payload which fails to DECODE touches nothing.
        self.client.reg_write({0x03: 0x99})            # known-good leading pair
        self._silent('REGister:WRITE "Aw"')            # then garbage: no decode
        self.assertEqual(self.dev.regs.get(0x03), 0x99,
                         "an earlier good write must survive a later bad command")

    def test_register_write_malformed_writes_nothing(self):
        # A payload that fails to decode at all writes nothing -- b64_decode
        # returns <=0 before any register is touched. This is the only real
        # "nothing written" guarantee, and it is about decoding, not pairs.
        before = dict(self.dev.regs)
        self._silent('REGister:WRITE "Aw"')             # unpadded -> -151
        self.assertEqual(self.client.query("SYSTem:ERRor:COUNt?"), "1")
        self.assertEqual(self.dev.regs, before)

    def test_dsp_preset_set_and_query(self):
        self.assertEqual(self.client.query("DIAG:DSP:PRESet?"), "0")
        self._silent("DIAG:DSP:PRESet 2")
        self.assertEqual(self.client.query("DIAG:DSP:PRESet?"), "2")
        self.assertEqual(self.dev.dsp_preset, 2)
        self._silent("DIAG:DSP:PRESet 0")
        self.assertEqual(self.client.query("DIAG:DSP:PRESet?"), "0")

    def test_dsp_preset_invalid_value_silent_but_queued(self):
        self._silent("DIAG:DSP:PRESet 7")               # not in {0,1,2}
        self.assertEqual(self.client.query("SYSTem:ERRor:COUNt?"), "1")
        # The code matters, not just the count: set_dsp() in csd_diag_scpi.c
        # pushes SCPI_ERROR_ILLEGAL_PARAMETER_VALUE (-224) for v > 2 because
        # csd_dsp_set_preset() treats anything >= 2 as "FIR preset" -- a
        # typo'd 7 would otherwise silently select the bass shelf.
        self.assertEqual(self.client.query("SYSTem:ERRor?", raise_error=False),
                         "-224,Illegal parameter value")
        self.assertEqual(self.client.query("DIAG:DSP:PRESet?"), "0")  # unchanged

    def test_error_queue_pop_and_count(self):
        self._silent('REGister:WRITE "Aw"')             # unpadded b64 -> -151
        self.assertEqual(self.client.query("SYSTem:ERRor:COUNt?"), "1")
        # reading the queue legitimately returns error-shaped text;
        # raise_error=False so it is returned, not raised.
        self.assertEqual(self.client.query("SYSTem:ERRor?", raise_error=False),
                         "-151,Invalid string data")
        self.assertEqual(self.client.query("SYSTem:ERRor:NEXT?", raise_error=False),
                         "0,No error")
        self.assertEqual(self.client.query("SYSTem:ERRor:COUNt?"), "0")


class HonestyRuleTests(unittest.TestCase, _QueueAssertions):
    """Stubbed command families must return an error, never a made-up value."""

    def setUp(self):
        self.dev = FakeDevice()
        self.client = csd_diag.CsdDiag(SimTransport(self.dev))

    def tearDown(self):
        self.client.close()

    def test_backing_table_shape(self):
        for pattern, kind, _why in BACKING:
            self.assertIn(kind, ("backed", "stub"), pattern)

    def test_stubs_are_rejected_never_invented(self):
        # The honesty rule that matters most: an unimplemented command must
        # never produce a plausible value. It is rejected silently and the
        # reason is queued as -113 Undefined header.
        for pattern, kind, _why in BACKING:
            if kind != "stub":
                continue
            sample = STUB_SAMPLES[pattern]
            depth, text = self._queued_error(sample)
            self.assertEqual(depth, "1", f"stub {pattern!r} was accepted")
            self.assertTrue(text.startswith("-113,"),
                            f"stub {pattern!r} queued {text!r}, not -113")
            self.assertNotRegex(text, r"^\+?\d\.\d+E[+-]\d{2}$",
                                f"stub {pattern!r} returned a value!")

    def test_measure_and_source_are_rejected(self):
        # Owner-limited command set: MEASure:/SOURce: must be refused, never
        # answered with an invented value.
        for cmd in ("MEASure:VAC:RMS? 1", "MEASure:VAC:FREQ? CH2",
                    "MEASure:THD? 2", "SOURce:VAC:AMPLitude? 1",
                    "SOURce:VAC:FREQuency? 2", "SOURce:VAC:STATe? CH1",
                    "SOURce:VAC:AMPLitude 1.5"):
            depth, text = self._queued_error(cmd)
            self.assertEqual(depth, "1", f"{cmd!r} was accepted")
            self.assertTrue(text.startswith("-113,"),
                            f"{cmd!r} queued {text!r}, not -113")

    def test_stubs_raise_device_error_by_default(self):
        # A rejected command is silent on the wire, so the client's job is to
        # surface the queued rejection rather than time out quietly.
        depth, text = self._queued_error("CONFigure:CURRent 1.0")
        self.assertEqual(depth, "1")
        self.assertIn("-113", text)

    def test_backed_commands_are_never_rejected(self):
        # The honesty rule in code. -113 Undefined header is what the device
        # queues for a header it does not implement, so a BACKED command that
        # produces one is the sim (or the firmware) claiming to support a
        # command it does not. Asserted via the queue because a rejection is
        # silent -- checking only the reply text would miss it entirely.
        for cmd in ("*IDN?", "*OPC?", "SYSTem:ERRor:COUNt?",
                    "SYSTem:ERRor?", "DIAG:DSP:PRESet?",
                    'REGister:READ? "AA=="', 'REGister:READ? "Aw=="'):
            self.client.query("SYSTem:ERRor:NEXT?", raise_error=False)
            reply = self.client.query(cmd, raise_error=False)
            self.assertFalse(reply.startswith("-113,") or reply.startswith("-201,"),
                             f"backed cmd {cmd!r} returned {reply!r}")
            depth = self.client.query("SYSTem:ERRor:COUNt?", raise_error=False)
            self.assertEqual(depth, "0",
                             f"backed cmd {cmd!r} queued a rejection")

    def test_unapproved_commands_are_rejected(self):
        for cmd in ("*RST", "*CLS"):                    # outside approved set
            depth, text = self._queued_error(cmd)
            self.assertEqual(depth, "1", f"{cmd!r} was accepted")
            self.assertTrue(text.startswith("-113,"),
                            f"{cmd!r} queued {text!r}, not -113")


class SimClientWireTests(unittest.TestCase):
    """Client<->sim transport-level cases: >64B reply, multi-packet, timeout."""

    def setUp(self):
        self.dev = FakeDevice()
        self.client = csd_diag.CsdDiag(SimTransport(self.dev))

    def tearDown(self):
        self.client.close()

    def _long_error_reply(self, pad):
        # A long bogus header makes the device echo it back inside a
        # '-201,Undefined header, "..."' reply longer than one 64B packet.
        return "BOGUS:" + "X" * pad

    def test_response_longer_than_64_bytes(self):
        # Widest reply the protocol produces: 64 registers -> 88 base64 chars,
        # which exceeds one 64-byte bulk IN packet. This is real device-shaped
        # output, not an echoed command -- the earlier version of this test
        # passed only because the sim echoed an error-shaped string back, so it
        # exercised the request path and nothing on the reply path.
        addrs = list(range(64))
        payload_b64 = base64.b64encode(bytes(addrs)).decode()
        self.assertEqual(len(payload_b64), 88, "reply no longer spans packets")
        reply = self.client.query('REGister:READ? "%s"' % payload_b64)
        self.assertGreater(len(reply), 64)
        self.assertGreater(len(pack_packets(build_sysex(reply), 64)), 1)
        # Reply must decode to exactly what the device holds for those 64
        # addresses -- derived from the fake's own state, never hardcoded, so
        # a change to the fake register map moves the expectation with it.
        # (bytes(64) here was wrong: addresses 0x00/0x03 are non-zero.)
        self.assertEqual(base64.b64decode(reply, validate=True),
                         bytes(self.dev.regs.get(a, 0) & 0xFF for a in addrs))

    def test_multi_packet_downlink_small_usb_packet(self):
        # Same reply across 12-byte bulk IN packets: the 88-char register dump
        # now arrives in ~11 packets, which is where a reassembly bug shows up.
        dev = FakeDevice(max_packet=12)              # 12-byte bulk IN packets
        client = csd_diag.CsdDiag(SimTransport(dev))
        addrs = list(range(64))
        reply = client.query('REGister:READ? "%s"'
                             % base64.b64encode(bytes(addrs)).decode())
        self.assertGreater(len(reply), 64)
        self.assertGreater(len(pack_packets(build_sysex(reply), 12)), 4,
                           "reply is not actually multi-packet; test proves nothing")
        client.close()

    def test_error_reply_raises_device_error(self):
        # Classification lives in read_reply, so it must be exercised, not
        # patched away: feeding bytes in at the TRANSPORT means the real
        # SysEx parse and the real error check run. Injecting a fake
        # read_reply (an earlier attempt) could never fail and proved nothing.
        class _OneShot:
            """Transport that answers once, AFTER the request goes out.

            Arming on send() matters: CsdDiag.query() drains the pipe via
            _flush() before it sends, so a transport that answers on the first
            recv has its payload swallowed by that flush and the test times
            out for a reason that has nothing to do with the classifier.
            """
            def __init__(self, text):
                self._armed = False
                self._data = b"".join(pack_packets(build_sysex(text)))

            def send(self, data):
                self._armed = True

            def recv(self, _n, _timeout):
                if not self._armed:
                    return b""
                d, self._armed, self._data = self._data, False, b""
                return d

            def close(self):
                pass

        client = csd_diag.CsdDiag(_OneShot("-113,Undefined header"))
        with self.assertRaises(DeviceError) as cm:
            client.query("WAT:IS:THIS?")
        self.assertIn("-113", cm.exception.text)
        client.close()

    def test_rejected_command_times_out_rather_than_returning_text(self):
        # The contract the bench forced: send a command the device refuses and
        # you get silence, not '-113,...'. A client or sim that returns error
        # text here would hide a real transport failure behind a green test.
        with self.assertRaises(DiagTimeout):
            self.client.query("WAT:IS:THIS?", timeout=0.3)

    def test_timeout_on_set_command_no_reply(self):
        # Set-commands are silent; querying one must time out.
        t0 = time.monotonic()
        with self.assertRaises(DiagTimeout):
            self.client.query('REGister:WRITE "Ayo="', timeout=0.2)
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_command_reaches_device_verbatim(self):
        self.client.command('REGister:WRITE "Ayo="')
        self.assertIn('REGister:WRITE "Ayo="', self.dev.commands_seen)


class RawFramingClientTests(unittest.TestCase):
    """CsdDiag over the raw-MIDI (rtmidi/ALSA) framing path."""

    def test_query_via_raw_framing(self):
        dev = FakeDevice(framing="raw")
        client = csd_diag.CsdDiag(SimTransport(dev), framing="raw")
        self.assertEqual(client.query("*IDN?"),
                         "ClearSound,CSD-AN22,SN0001,1.0.0-sim")
        client.close()


class DescriptorParseTests(unittest.TestCase):
    """find_midi_bulk_endpoints on a hand-built USB-MIDI config blob."""

    @staticmethod
    def _ep(addr, attr=0x02):
        return bytes([0x07, 0x05, addr, attr, 0x40, 0x00, 0x00])

    @staticmethod
    def _cs_ep_general():
        return bytes([0x05, 0x25, 0x01, 0x01, 0x01])

    def test_finds_midi_bulk_pair(self):
        cfg = (self._ep(0x01) + self._cs_ep_general() +
               self._ep(0x82) + self._cs_ep_general())
        self.assertEqual(find_midi_bulk_endpoints(cfg), (0x01, 0x82))

    def test_ignores_non_midi_bulk(self):
        cfg = self._ep(0x03) + self._ep(0x84)       # bulk but no CS_ENDPOINT
        self.assertEqual(find_midi_bulk_endpoints(cfg), (None, None))

    def test_ignores_isochronous_with_cs(self):
        cfg = (self._ep(0x01, attr=0x01) + self._cs_ep_general() +
               self._ep(0x82, attr=0x01) + self._cs_ep_general())
        self.assertEqual(find_midi_bulk_endpoints(cfg), (None, None))


class CliTests(unittest.TestCase):
    def test_cli_query_against_sim(self):
        rc = csd_diag.main(["--sim", "--backend", "sim", "*IDN?"])
        self.assertEqual(rc, 0)

    def test_cli_rejected_query_reports_the_queued_reason(self):
        # A refused query must NOT be reported as a bare TIMEOUT: the reason is
        # in the error queue and the operator needs it. rc 1 = "device refused",
        # rc 2 = "device silent with nothing queued" (a transport problem).
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = csd_diag.main(["--sim", "--backend", "sim", "CONFigure:CURRent?"])
        self.assertEqual(rc, 1)
        self.assertIn("REJECTED", buf.getvalue())
        self.assertIn("-113", buf.getvalue())

    def test_cli_silent_set_command_ok(self):
        # a set-command (no '?' anywhere) must not time out the CLI
        rc = csd_diag.main(["--sim", "--backend", "sim",
                            "DIAG:DSP:PRESet 1"])
        self.assertEqual(rc, 0)

    def test_cli_query_with_argument_mid_command(self):
        # queries with '?' before the quoted payload still route as queries
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = csd_diag.main(["--sim", "--backend", "sim",
                                'REGister:READ? "Aw=="'])
        self.assertEqual(rc, 0)
        # Verbatim reply, quotes included: SCPI_ResultCharacters emits a
        # quoted string on the wire (csd_diag_scpi.c:345), and the CLI prints
        # what the device sent. Stripping quotes is the reg_read() helper's
        # job, not the CLI's -- a CLI that tidied replies would hide exactly
        # the framing difference the raw-path tests exist to catch.
        self.assertEqual(buf.getvalue().strip(), "MA==")


if __name__ == "__main__":
    unittest.main(verbosity=2)
