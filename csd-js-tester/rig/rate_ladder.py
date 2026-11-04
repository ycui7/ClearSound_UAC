#!/usr/bin/env python3
"""Rate ladder in ONE process, ONE device boot.

Why one boot: each run_xe.sh reload resets the device, so a per-leg reload lets
clock/DPLL state drift between legs and turns a rate effect into a boot effect.
Here the image loads once and only CSD_RATE changes between legs -- the same
strict-A/B discipline used elsewhere: every launch argument identical, one
variable flipped.

Reads the probe's own verdict lines so there is exactly one implementation of
the measurement, not a re-typed copy that can drift from it.
"""
import importlib
import io
import os
import sys
import contextlib

sys.path.insert(0, "rig")

RATES = [48000, 96000, 192000, 384000]
PRESET = os.environ.get("LADDER_PRESET", "2")


def run_rate(mod, rate):
    os.environ["CSD_RATE"] = str(rate)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            mod.main()
    except SystemExit:
        pass
    except Exception as e:                      # noqa: BLE001
        return f"  LEG ERROR {type(e).__name__}: {e}"
    keep = ("RATE under test", "kernel-granted", "DEBUG:I2S", "host wrote",
            "device puts", "device takes", "put sites", "fill=", "zero runs",
            "most common", "ABORT", "NOT-PROVEN")
    return "\n".join("  " + ln.strip() for ln in buf.getvalue().splitlines()
                     if any(k in ln for k in keep))


def main():
    import probe_zero_pattern as mod
    # The probe parses its own argv for --dsp; supply it here rather than
    # forking, so one boot serves every leg.
    sys.argv = ["rate_ladder.py", "--dsp", PRESET]
    for rate in RATES:
        print(f"\n########## CSD_RATE={rate:,}  preset={PRESET} ##########")
        print(run_rate(mod, rate), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
