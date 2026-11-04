#!/usr/bin/env python3
"""Strict A/B: ONE image, ONE boot, only runtime levers flipped between legs.

WHY THIS REPLACES THE EARLIER COMPARISONS. Earlier numbers were read off
different binaries and then compared as if they were an A/B:
  put-ratio 0.81  release build, preset 0
  put-ratio 0.72  debug build,  MAX_MIX_COUNT=0
  put-ratio 0.64  debug build,  preset 2, MAX_MIX_COUNT=2
Release-vs-debug and one-build-per-flag are whole-image differences, so the
conclusion "removing the mixer made the census worse" was never controlled. This
harness removes that error: the image loads once and legs differ ONLY in values
settable at runtime -- DIAG:DSP:PRESet and whether the capture stream is opened.

Levers deliberately left ALONE because they are not runtime-settable:
  CSD_GEN_INSTRUMENT (compile-time) -- already known to be the dominant cost and
      excluded from every image measured here.
  MAX_MIX_COUNT (compile-time) -- needs its own single-variable, same-image pair
      once the runtime levers are exhausted.

Legs:
  1  preset 2, duplex          -- the shipping shape
  2  preset 0, duplex          -- FIR removed from the audio core, same image
  3  preset 0, playback only   -- plus capture-direction channel traffic removed
  4  preset 2, duplex          -- repeat of leg 1: a run that drifts vs leg 1 is
                                  drift, not a DSP effect. Without this, any
                                  leg-to-leg difference is unfalsifiable.

EFFECTIVE LRCLK (puts/channel per second) is the quantity: incremented by the
audio core once per LRCLK frame, independent of USB pacing and of the forced
feedback endpoint.
"""
import contextlib
import io
import os
import sys

sys.path.insert(0, "rig")

RATE = os.environ.get("LEGS_RATE", "384000")
LEGS = [
    ("1 preset2 duplex", "2", False),
    ("2 preset0 duplex", "0", False),
    ("3 preset0 playonly", "0", True),
    ("4 preset2 duplex (repeat of 1)", "2", False),
]
KEEP = ("EFFECTIVE LRCLK", "USB side wrote", "device puts", "put sites",
        "DEBUG:I2S", "fill=", "zero runs", "most common", "capture stream",
        "ABORT", "NOT-PROVEN")


def leg(mod, preset, no_cap):
    sys.argv = ["x", "--dsp", preset] + (["--no-capture"] if no_cap else [])
    os.environ["CSD_RATE"] = RATE
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            mod.main()
    except SystemExit:
        pass
    except Exception as e:                          # noqa: BLE001
        return f"  LEG ERROR {type(e).__name__}: {e}"
    return "\n".join("  " + ln.strip() for ln in buf.getvalue().splitlines()
                     if any(k in ln for k in KEEP))


def main():
    import probe_zero_pattern as mod
    print(f"### ONE image, ONE boot, {RATE} Hz -- runtime levers only ###")
    out = {}
    for name, preset, no_cap in LEGS:
        print(f"\n--- leg {name} ---")
        txt = leg(mod, preset, no_cap)
        print(txt, flush=True)
        for ln in txt.splitlines():
            if "EFFECTIVE LRCLK" in ln:
                out[name] = ln.strip()
    print("\n=== summary (the only controlled comparison so far) ===")
    for name, _p, _n in LEGS:
        print(f"  {name:<32} {out.get(name, 'NO READING')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
