#!/usr/bin/env python3
"""Fit the device's product-rounding rule through the VALIDATED twin.

Why this script and not the two earlier ones: _round_fit.py and _pre_stage.py
re-implemented the filter in Python and scored NOTHING because they fed it the
24-bit schedule field unscaled -- the device filters Q31 samples, and the
validated pipeline scales by 256 (<<8) before the filter, exactly as
filtered_expectation() does. An unscaled model sits 2^8 below the capture, so
every candidate scored identically. Their printed verdicts are void.

This one drives the SAME binary the rig uses (dsp_bq_host, the one
dsp_model_selftest.py pins bit-for-bit to the vendor's process_xcore) and only
flips its rounding-mode argument. Everything else -- scaling, state init,
saturation, int40 accumulator -- stays identical to the run that produced the
101 dB residual. One variable, as it should be.

The capture is the saved 800 MHz preset-2 run (tap words, 24-bit fields).
Compare at delta 146, the order established by the rare-value spike. If one
mode drives exact-equality to ~100%, that is the rule the silicon implements
and the word-exact claim closes.
"""
import os
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import one_shot_block as O

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dsp_bq_host")
N_FRAMES = 23_068_672
COEFFS = [538340238, -1075347991, 537009399, 2145602141, -1071861964]
LSH = 0
DELTA = 146
OFF = 1 << 20
NF = 1 << 20

cap = np.load("/tmp/dsp_cap.npy")
gotL = cap[0].astype(np.uint32)[OFF:OFF + NF]
gotR = cap[1].astype(np.uint32)[OFF:OFF + NF]
del cap

sched_cache = "/tmp/dsp_sched24.npy"
if os.path.exists(sched_cache):
    sch = np.load(sched_cache)
else:
    L, R, _ = O.build_block(N_FRAMES)
    sch = np.stack([L.astype(np.int32), R.astype(np.int32)])
    np.save(sched_cache, sch)
print(f"schedule {sch.shape}, capture window {NF:,} frames at offset {OFF:,}, "
      f"delta {DELTA}")

pairs = np.empty((N_FRAMES, 2), dtype=np.int32)
pairs[:, 0] = (sch[0].astype(np.uint32) << np.uint32(8)).astype(np.int32)
pairs[:, 1] = (sch[1].astype(np.uint32) << np.uint32(8)).astype(np.int32)
inf, outf = "/tmp/fit_in.bin", "/tmp/fit_out.bin"
pairs.tofile(inf)
del pairs

MODES = {0: "half up (shipped / vendor model)", 1: "truncate toward zero",
         2: "arithmetic shift (floor)", 3: "round half to even"}

print("\nmode                          median|d|  RMS resid   %exact    %|d|<=1  SNR")
sig = float(np.sqrt((gotL.astype(np.float64) ** 2).mean()))
results = {}
for mode, name in MODES.items():
    r = subprocess.run([EXE, inf, outf] + [str(c) for c in COEFFS] +
                       [str(LSH), str(mode)], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"twin failed mode {mode}: {r.stderr.strip()}")
    out = np.fromfile(outf, dtype=np.int32).reshape(-1, 2)
    stats = []
    for got, col in ((gotL, 0), (gotR, 1)):
        exp24 = (out[OFF + DELTA: OFF + DELTA + NF, col].astype(np.uint32)
                 >> np.uint32(8))
        d = got.astype(np.int64) - exp24.astype(np.int64)
        stats.append((float(np.median(np.abs(d))),
                      float(np.sqrt((d ** 2).mean())),
                      float((d == 0).mean()) * 100.0,
                      float((np.abs(d) <= 1).mean()) * 100.0))
    med = max(s[0] for s in stats)
    rms = max(s[1] for s in stats)
    ex = min(s[2] for s in stats)
    o1 = min(s[3] for s in stats)
    snr = 20 * np.log10(sig / max(rms, 1e-9))
    results[mode] = ex
    print(f"{name:28s} {med:9.0f} {rms:10.1f} {ex:8.3f} {o1:9.3f} {snr:6.1f} dB")

best = sorted(results, key=lambda k: -results[k])[0]
print(f"\nbest exact-match: mode {best} ({MODES[best]}) at {results[best]:.3f}%")
if results[best] > 99.0:
    print("==> That mode IS the silicon's rounding rule. The shipped twin's mode "
          "0 is NOT it; fix the model (and re-run the selftest against the "
          "vendor model before trusting either).")
elif results[0] == max(results.values()):
    print("==> Mode 0 (the shipped, vendor-matching rule) remains the best fit; "
          "rounding is not the missing piece. Next oracle: the real asm under "
          "xsim, since every host model agrees with every other host model.")
