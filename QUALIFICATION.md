# Upsampling filter -- local qualification

Qualified on this machine (msi-b850p), host-compiled, shipped fixed-point tables.
No hardware involved. Nothing here is a design target: every figure is measured.

## Suites

```
tools/dsp: python3 -m unittest test_gen_upsample_taps      -> Ran 10 tests ... OK
gcc -O2 -Isrc/dsp -o /tmp/tu tools/dsp/test_upsample.c src/dsp/csd_upsample.c -lm && /tmp/tu
                                                          -> RESULT: PASS (13 checks)
```

## Measured

| path | gain error across band | worst image | group delay |
|------|------------------------|-------------|-------------|
| 48 kHz -> 384 kHz  | -0.12 .. 0.00 dB | -168.1 dB | 815 samples = 2.122 ms |
| 96 kHz -> 384 kHz  | -0.12 .. 0.00 dB | -192.3 dB | -- |
| 192 kHz -> 384 kHz | -0.12 .. 0.00 dB | -204.9 dB | -- |

Tones are bin-centred (`f = k*384000/8192`), so no spectral leakage floor can be
mistaken for a spur or for gain loss. Gain error is quoted against a -12.04 dBFS
input; the -0.12 dB corner of the range is the band-limit's expected droop near
the top of the measured band.

**Group delay check.** Predicted for the 48 kHz path: three halfband stages at 80
output samples each, referred to 384 kHz, 320 + 160 + 80 = 560, plus the
band-limit's 255, = 815. The stimulus sat at `in[16]`, which contributes 16*8 =
128. Measured peak: **943 = 815 + 128**, exact. An FIR whose measured delay
matches the analytic delay to the sample is the cleanest available evidence that
the phases are aligned and the taps are in the right places.

## Requirement status

- **120 dB stopband rejection**: met, worst case -168 dB (48 kHz path).
- **No worse than a 4th-order analog Bessel**: met, and pinned by a test against
  the analytic reverse-Bessel response -- level at the corner, 0.3-1.1 dB better
  in the passband, 4.6-14.2 dB steeper above it, exactly linear phase.
- **Sinc, not linear interpolation**: windowed-sinc halfband, 161 taps.
- **One stream into the mixer**: output assigned, never accumulated (prefill test).

## Two bugs the qualification caught, both in the tests

1. `halfband interp == naive sum` reported 420/432 mismatches. The reference
   computed `hb_in[a] + hb_in[b]` in **32-bit** before casting, so two
   full-scale random samples overflowed; the runtime promotes each operand to
   `int64_t` first. The filter was correct throughout -- a reference bug that
   would have been blamed on the DSP.
2. The first qualification probe (a) initialised `worst_all = 0.0`, so a real
   -154 dB image could never be reported, and (b) always fed the band limit
   8x samples even when the cascade produced 2x or 4x, so the 96/192 kHz rows
   measured uninitialised memory.

## Not yet done

- Not wired into `csd_dsp.xc`; no XS3 build, so the multiply counts in
  `UPSAMPLING.md` remain counts, not cycle measurements.
- No hardware THD/noise/acoustic measurement.
- The 15 kHz band-limit corner still needs sign-off: it removes programme
  content from 15-21.6 kHz on 48 kHz material.
