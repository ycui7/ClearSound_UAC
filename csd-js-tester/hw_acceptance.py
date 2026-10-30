#!/usr/bin/env python3
"""Hardware acceptance matrix for the SCPI-over-USB-MIDI diagnostic interface.

Every command the firmware claims to support is exercised against the real
device on the raw bulk endpoints (the path proven to round-trip), and each
check prints PASS/FAIL with the observed value. Exit code is non-zero if any
check fails, so this file is the definition of 'in and out working'.

Two backends, because they answer different questions and one needs no root:

  rawmidi (default)  the regular kernel MIDI node, /dev/snd/midiC<D>D<P>. This
          is the path the bench uses day to day, needs no sudo, and speaks raw
          MIDI bytes -- the same framing Chrome's Web MIDI hands over, so a
          pass here is evidence about the browser path too.
  pyusb              claims the MIDI interface off the kernel and talks 4-byte
          USB-MIDI events on the bulk endpoints. Needs root, and it is the only
          way to see the framing the firmware actually puts on the wire.

Usage:
    <tree>/.venv/bin/python hw_acceptance.py            # rawmidi
    sudo <tree>/.venv/bin/python hw_acceptance.py --pyusb
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import csd_diag as C

results = []

def check(name, got, want=None, ok=None):
    # ok(got) form first: a lambda is always truthy, so appending it un-called
    # would report every check as PASS.
    #
    # Replies arrive CRLF-terminated (the personality emits "\r\n"), so compare
    # stripped. Without this, a correct device failed: got '0\r\n' want '0'
    # while the device had answered exactly right.
    if ok is not None:
        passed = bool(ok(got))
    elif want is not None and isinstance(got, str):
        passed = (got.strip() == want)
    else:
        passed = (got == want)
    results.append((passed, name, repr(got) if want is None else "%r == %r" % (got, want)))
    print("%-4s %-34s %s" % ("PASS" if passed else "FAIL", name,
                             repr(got) if passed else "got %r want %r" % (got, want)))

def main():
    use_pyusb = "--pyusb" in sys.argv[1:]
    if use_pyusb:
        t = C.PyUsbTransport(0x20B1, 0x0018)
        d = C.CsdDiag(t, framing="usb-midi")
        print("backend: pyusb raw bulk (usb-midi framing), needs root")
    else:
        t = C.RawMidiTransport()
        d = C.CsdDiag(t, framing="raw")
        print("backend: kernel raw MIDI node (raw MIDI framing), no root")

    # 1. *IDN? -- identity string, proves both directions and the parser.
    check("*IDN? format",
          d.query("*IDN?"),
          ok=lambda s: s.startswith("ClearSound,CSD-"))

    # 2. DSP preset: set/get round trip over the real state machine.
    for p in (0, 1, 2, 0):
        d.command("DIAG:DSP:PRESet %d" % p)
        time.sleep(0.05)
        check("PRESet %d readback" % p, d.query("DIAG:DSP:PRESet?"), str(p))

    # 3. Out-of-range preset must be rejected, and rejected LOUDLY.
    #
    # NOTE: this must be a command(), not a query(). A set-command produces no
    # reply at all on this transport -- the rejection surfaces in the error
    # queue (checked next), which is the SCPI convention this personality
    # follows. Querying it waits for a reply that never comes and raises
    # DiagTimeout, which previously read as "the transport is broken" when the
    # device behaviour was in fact correct.
    d.command("DIAG:DSP:PRESet 7")
    time.sleep(0.05)

    # 4. Error queue: the bad preset above must now be queued; pop it, then
    #    confirm the queue drains to empty.
    e1 = d.query("SYSTem:ERRor:NEXT?", raise_error=False)
    check("PRESet 7 rejected (queued error)", e1, ok=lambda s: s.startswith("-"))
    n = d.query("SYSTem:ERRor:COUNt?")
    check("ERR:COUNt drained", n, "0")

    # 5. Unknown command -> -113 undefined header, in the ERROR QUEUE.
    #
    # Third instance of the same test bug (see PRESet 7 above): a command the
    # parser does not know produces NO reply -- the rejection is queued and
    # read out with SYSTem:ERRor:NEXT?. Querying it and waiting for a reply
    # raised DiagTimeout, which read as "the transport died" and aborted the
    # matrix before the register checks ever ran.
    d.command("BOGUS:CMD?")
    time.sleep(0.05)
    e2 = d.query("SYSTem:ERRor:NEXT?", raise_error=False)
    check("unknown cmd -> -113 queued", e2, ok=lambda s: s.startswith("-113"))

    # 6. Register proxy over the second I2C client (base64 payloads, BATMCU
    #    convention). Strict on purpose: the previous version accepted any
    #    reply that "looked like hex OR started with -", which made every
    #    register check pass by RETURNING AN ERROR -- the exact bug we were
    #    chasing looked green here. A rejected command must fail the run.
    try:
        vals = d.reg_read([0x00, 0x03])
        check("REG READ [0x00,0x03] returns 2 bytes", repr(vals),
              ok=lambda s: len(vals) == 2)
    except Exception as e:
        check("REG READ [0x00,0x03] returns 2 bytes", repr(e), ok=lambda s: False)

    # Write-then-read must actually agree. A write that silently failed shows
    # up here as a mismatch, where the old test could not see it at all.
    d.reg_write({0x03: 0x00})
    time.sleep(0.05)
    back = d.reg_read(0x03)
    check("REG WRITE 0x03=0 then READ agrees", repr(back),
          ok=lambda s: back == bytes([0x00]))

    # A register batch is one round trip; assert the device answers a range.
    rng = d.reg_read([0x00, 0x01, 0x02, 0x03])
    check("REG READ 4-address range returns 4 bytes", repr(rng),
          ok=lambda s: len(rng) == 4)

    # 7. Back-to-back queries in one session: catches the stale-accumulator
    #    bug where one truncated reply poisoned every later command.
    got = [d.query("*IDN?") for _ in range(5)]
    check("5x IDN stable", got, ok=lambda g: len(set(g)) == 1 and g[0].startswith("ClearSound"))

    # 8. Malformed payload must be REFUSED with nothing written. It is a set-
    #    style failure: b64_decode returns <=0, the callback pushes
    #    SCPI_ERROR_INVALID_STRING_DATA (-151) and returns SCPI_RES_ERR, which
    #    emits NO reply -- so this must be polled through the error queue, not
    #    awaited as a query. (Awaiting it looks exactly like a dead transport;
    #    that trap cost two rounds of debugging on the framing path.)
    d.query("SYSTem:ERRor:NEXT?", raise_error=False)          # drain
    d.command('REGister:READ? "Aw"')                           # unpadded
    n = d.query("SYSTem:ERRor:COUNt?", raise_error=False)
    check("unpadded base64 queued an error", n, ok=lambda s: s.strip() != "0")
    e = d.query("SYSTem:ERRor?", raise_error=False)
    check("rejection is -151 invalid string data", e, ok=lambda s: s.startswith("-151"))

    t.close()
    bad = [r for r in results if not r[0]]
    print("\n%d/%d checks passed" % (len(results) - len(bad), len(results)))
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
