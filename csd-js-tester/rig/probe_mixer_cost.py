#!/usr/bin/env python3
"""Where do the 781 ticks per half-frame actually go?

No more rebuild-and-guess. mixer.xc already carries hardware-timer
instrumentation: a tile[1] `timer` is read at the top of mixer1's sample loop
and either side of the exchange with audio() (mixer.xc:527-662), and the peaks
are published as DEBUG:MIXer:BUSY? / DEBUG:MIXer:TAIL?.

Units, from the instrument's own comment (csd_diag_scpi.c:1311): 12500 ticks is
"a whole 48 kHz sample period", so the tile timer runs at 600 MHz -> 1 tick =
1.667 ns. The budget this is judged against:

    half-frame at 384 kHz = 32 BCLK = 1.302 us = 781 ticks
    full frame at 384 kHz = 64 BCLK = 2.604 us = 1563 ticks

BUSY  = peak ticks mixer1 held audio() between the request token and the
        sample exchange. TAIL = peak ticks mixer1 spent AFTER unblocking
        audio(), before it could serve the next request.
The code states the deadline rule: worst case is max(BUSY, TAIL) and
BUSY+TAIL bounds the whole mixer loop. So if BUSY+TAIL approaches or exceeds
1563, the mixer handshake is by itself the entire frame budget and the missing
~36% of LRCLK frames are explained without reference to the FIR, the tap, the
DSP preset or the host.

Debug-only rows, so run under the debug+tap image (/tmp/xe_DBG_TAP.xe).
"""
import os
import sys

sys.path.insert(0, "rig")

RATE = int(os.environ.get("CSD_RATE", "384000"))
TILE_HZ = 600_000_000          # from the 12500-tick == one 48 kHz period note
BCLK_PER_FRAME = 64            # 2 ch x 32-bit subslot, master I2S
BUDGET_FRAME = round(TILE_HZ / RATE * (BCLK_PER_FRAME / 64.0))  # ticks/frame


def main():
    import probe_zero_pattern as mod

    print(f"### mixer handshake cost at {RATE} Hz, one boot ###")
    print(f"  budget: {BUDGET_FRAME} ticks/frame "
          f"({BUDGET_FRAME/2} per half-frame)")

    # One streaming leg, then read the peaks. Peaks are max-hold since boot and
    # only cleared at init, so a single boot gives the true worst case -- no
    # reset needed, and nothing to re-run if the first leg is low.
    sys.argv = ["x", "--dsp", "2"]
    os.environ["CSD_RATE"] = str(RATE)
    import contextlib, io
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            mod.main()
    except SystemExit:
        pass
    except Exception as e:                                  # noqa: BLE001
        print(f"  LEG ERROR {type(e).__name__}: {e}")
    fill = [ln.strip() for ln in buf.getvalue().splitlines() if "fill=" in ln]
    print("  stream:", fill[0] if fill else "no fill line")

    for label, cmd in (("BUSY  (mixer held audio())", "DEBUG:MIXer:BUSY?"),
                       ("TAIL  (after unblocking) ", "DEBUG:MIXer:TAIL?"),
                       ("FRAMES(mixer1 loop)      ", "DEBUG:MIXer:FRAMES?"),
                       ("HWCfg                    ", "DEBUG:MIXer:HWCfg?")):
        try:
            v = mod.scpi(cmd)
        except Exception as e:                              # noqa: BLE001
            v = f"ERR {type(e).__name__}: {e}"
        print(f"  {label} = {v!r}")

    # These rows are compiled out twice over: they live under
    # #ifndef SPECIALTYCIRCUITS_RELEASE (so a RELEASE image answers empty) AND
    # their producers sit under #if defined(CSD_GEN_INSTRUMENT) (so a build
    # without that flag answers 0 -- which is what first fooled this probe into
    # reporting "fits with margin" from counters that were never in the image).
    # An absent instrument is VOID, never zero and never evidence of clean.
    def read(cmd):
        s = mod.scpi(cmd).strip()
        if not s or s.lower().startswith("err"):
            return None
        try:
            return int(s.split()[0].rstrip(","))
        except ValueError:
            return None

    busy, tail, frames = (read("DEBUG:MIXer:BUSY?"),
                          read("DEBUG:MIXer:TAIL?"),
                          read("DEBUG:MIXer:FRAMES?"))
    if frames is None or frames == 0:
        print("\n  VERDICT: VOID -- MIXer:FRAMES? did not answer with a nonzero"
              "\n  count, so this image has no mixer instrumentation (needs a"
              "\n  DEBUG build with -DCSD_GEN_INSTRUMENT). No conclusion about"
              "\n  the handshake is available from this run.")
        return 2
    if busy is None:
        print("\n  VERDICT: VOID -- BUSY unreadable while FRAMES is live.")
        return 2

    print(f"\n  mixer1 ran {frames:,} sample loops; BUSY peak = {busy} ticks "
          f"of the {BUDGET_FRAME//2}-tick half-frame "
          f"({busy / (BUDGET_FRAME / 2):.2f} of it)")
    # TAIL is NOT a peak and must never be compared to the budget: t1 is
    # refreshed only in the !cmd_pending_flag branch (mixer.xc:661), so
    # (t2 - t1) spans however many frames the decouple kept the mixer busy --
    # it is time-since-last-exchange. Quoting it against 1562 produced a
    # previous run's meaningless "85853x the frame budget".
    print(f"  TAIL = {tail} ticks -- NOT a peak (t1 refreshes in only one"
          "\n  branch), it is time-since-last-exchange. Reported, never scored.")
    half = BUDGET_FRAME / 2
    if busy >= half:
        print("  VERDICT: the handshake alone consumes the half-frame. That IS"
              "\n  the deficit; the FIR/tap are bystanders.")
    elif busy >= half // 2:
        print("  VERDICT: the handshake is over half the half-frame -- little"
              "\n  margin left for the FIR on top.")
    else:
        print("  VERDICT: the handshake fits with margin. The deficit is NOT"
              "\n  the handshake; look elsewhere.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
