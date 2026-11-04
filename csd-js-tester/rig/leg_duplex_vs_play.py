#!/usr/bin/env python3
"""One boot, two legs: duplex vs playback-only. Decides where the I2S deficit is.

The audio core runs on tile[1] while the USB/decouple side runs on tile[0]
(AUDIO_IO_TILE=1, XUD_TILE=0), so every sample exchange is a cross-tile channel
operation -- two outuint and two inuint per 2.6 us frame at 384 kHz, against a
tile[0] that is at 32/32 chanends and also hosts the XUD task.

Two candidate causes, opposite fixes:
  A) the audio core runs out of CPU cycles doing DSP/mixer work
     -> removing that work should fix it. It did not: DSP bypass moved the
        put-ratio 0.65 -> 0.81, and removing the mixer math entirely (
        MAX_MIX_COUNT=0) only reached 0.72 while making the LRCLK deadline
        census WORSE (71% late vs 56%). Less work did not buy the rate back.
  B) the audio core is BLOCKED on the cross-tile channel to tile[0]
     -> removing work is irrelevant; the core is waiting. Closing the capture
        stream halves the per-frame channel traffic on that one channel
        (c_aud carries both directions).

So this harness runs the SAME image twice in one process, one boot:
  leg 1: playback + capture (duplex, the production shape)
  leg 2: playback only      (capture stream never opened)
put()/take() are incremented by the audio core itself and need no ADC path, so
the LRCLK rate stays measurable in leg 2.

Reading the result:
  leg 2 LRCLK ~= negotiated rate -> cause B: contention on c_aud / tile[0].
  leg 2 LRCLK ~= leg 1           -> cause A is back in play; the deficit is
                                    intrinsic to the audio core's work loop.
"""
import contextlib
import importlib
import io
import os
import sys

sys.path.insert(0, "rig")

PRESET = os.environ.get("LEGS_PRESET", "2")
RATE = os.environ.get("LEGS_RATE", "384000")
KEEP = ("RATE under test", "kernel-granted", "capture stream", "kernel feedback",
        "EFFECTIVE LRCLK", "USB side wrote", "device puts", "device takes",
        "put sites", "DEBUG:I2S", "fill=", "zero runs", "most common",
        "ABORT", "NOT-PROVEN")


def leg(mod, argv):
    sys.argv = argv
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
    base = ["x", "--dsp", PRESET]
    print(f"### image legs at {RATE} Hz, preset {PRESET} ###")
    print("\n--- leg 1: DUPLEX (playback + capture) ---")
    print(leg(mod, base), flush=True)
    print("\n--- leg 2: PLAYBACK ONLY (capture never opened) ---")
    print(leg(mod, base + ["--no-capture"]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
