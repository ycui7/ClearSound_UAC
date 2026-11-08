# Upsampling filter -- detailed summary

Branch `upsampling`. Status: DSP core complete and tested off-target; **not yet
wired into `csd_dsp.xc`, never run on an XS3.**

## 1. Signal path

One stream per channel. The upsampler REPLACES the incoming stream; the mixer
receives exactly one stream per channel. Bypass and cascade are mutually
exclusive -- if the DSP is off the mixer takes the original stream and none of
this runs.

```
                        stage A (2x halfband cascade)                 stage B
 48k ──┐
       ├──[2x]──96k──[2x]──192k──[2x]──┐
 96k ──┼────────────[2x]──192k──[2x]──┤
192k ──┴─────────────────────[2x]─────┤
                                      │
                                      ▼
                               ┌──────────────┐   384k    ┌────────────┐  384k
                               │ 384k stream  │──────────▶│ band limit │──────▶ mixer
                               └──────────────┘           │ 511-tap    │  (one
                                                          │ fc 15 kHz  │   stream)
                                                          └────────────┘
```

Per rate: 48 kHz runs three 2x stages, 96 kHz two, 192 kHz one. Every stage is
the same 127-tap halfband kernel, so all three paths share one table and one
routine.

## 2. Stage A -- 2x halfband, structurally sparse

Kernel: 127 taps, symmetric. Nonzero taps sit at the **even** indices (64 of
them); the **odd** indices are exact zeros (62 of them) plus the centre tap at
index 63. That is the property that saves the work: zeros are never loaded.

Phase mapping (this is the part that is easy to get backwards -- the centre index
63 is ODD, so the identity branch lands on the odd output samples):

```
  x[n] ──┬────────────────────────────────────────▶ y[2n+1] = x[n-31]      (identity, delayed 31)
         │
         └──▶ 32 symmetric pairs over h[2k] ───────▶ y[2n]                    (interpolated)
              sum k=0..31  h[2k] * (x[n-k] + x[n-63+k])
```

Cost per input sample per stage: **32 multiplies + 1 delay**, against 127 for a
generic polyphase 2x. The interpolated taps are normalised so they sum to 1.0,
because the identity branch is exactly unity and the two phases must carry equal
gain.

Why the delay exists: the interpolated branch spans 64 taps, its group delay is
31.5 input samples. Without the 31-sample delay on the identity branch the two
phases are misaligned, and interleaving them comb-filters the signal. Measured
before the fix: DC still unity, but a 1125 Hz tone came out 1.5 / 9.1 / 12.8 dB
low for 192/96/48 kHz -- one stage worth of error each.

## 3. Stage B -- linear-phase 4th-order-equivalent band limit

511 taps, symmetric, at 384 kHz. Magnitude follows a 4th-order Butterworth with
a 15 kHz corner (0 dB DC, -3 dB at 15 kHz, -48 dB at 60 kHz, monotone); phase is
exactly linear, group delay flat at 255 samples. A real 4th-order IIR would have
frequency-dependent group delay, which the requirement excludes.

Folded on its symmetry -- 511 taps become 256 multiplies per output sample:

```
 acc = sum k=0..254  c[k] * (hist[pos-k] + hist[pos-(510-k)])  +  c[255]*hist[pos-255]
```

## 4. Numeric format and memory

- coefficients Q30 `int32`, product in `int64`, one `>>30` at the end
- samples `int32`, saturated on output, never wrapped
- no allocation anywhere: state is plain structs, suitable for tile statics
- state per channel: stage A `3 x 64 int32` = 768 B, stage B `511 int32` = 2044 B,
  total **2.8 KB**; scratch per call is `2 x 4 x n_in int32` for the 48 kHz path

## 5. Cost

| path | stage A MAC/s/ch | stage B MAC/s/ch | total MAC/s/ch |
|------|------------------|------------------|----------------|
| 48 kHz  | 32 x (48k+96k+192k) = 10.7 M | 256 x 384k = 98.3 M | 109 M |
| 96 kHz  | 32 x (96k+192k) = 9.2 M     | 98.3 M              | 107 M |
| 192 kHz | 32 x 192k = 6.1 M           | 98.3 M              | 104 M |

Stage B dominates: it runs at 384 kHz. A stereo pair is about 218 M MAC/s, i.e.
roughly a quarter of one 800 MHz XS3 tile before loop overhead -- but this is a
count, not a measurement; no cycle count exists until it runs on target.

For comparison, the 512-tap single-rate polyphase prototype it replaced cost
24.6 M MAC/s/ch in stage A, so the sparse halfband cascade is **2.3x cheaper**
there.

## 6. Measured results (host, shipped fixed-point tables)

```
DC gain                        1.0000
impulse peak                   319   (linear-phase delay, expected 319)
1 kHz tone through the 8x path  -12.04 dBFS   (in -12.04)
image at 47 kHz                -140.9 dB      (512-tap prototype)
band-limit DC                  1.000
folding vs unfolded            0 mismatches of 4096
halfband identity == x[n-31]   0 mismatches of 481
halfband interp == naive sum   0 mismatches of 448
output assigned not accumulated 0 of 1024 differ from a prefilled buffer
cascade 192/96/48 kHz           -12.04 dBFS each (unity, all three paths)
```

**Image rejection of the halfband cascade is weaker than the prototype's.**
Measured through the three-stage 48 kHz path with a 10 kHz tone: the 38 kHz spur
sits 60.6 dB below the tone and the 58 kHz spur 68.0 dB below it, against
140 dB for the 512-tap prototype. The 127-tap halfband was designed for 80 dB;
the cascade does not reach it. A longer halfband kernel is the lever, at 2x the
multiplies per stage for roughly double the length. **This is an open decision:**
accept ~61 dB with the cheap cascade, or spend taps to get back toward 80 dB.

## 7. Test inventory

`tools/dsp/test_gen_upsample_taps.py` -- 8 property tests on the generated
tables: DC gain, symmetry (=> linear phase), image rejection, passband flatness,
4th-order magnitude tracking, monotonicity, sinc-vs-linear margin.

`tools/dsp/test_upsample.c` -- 12 host checks on the runtime, compiled with gcc:
the behavioural numbers above plus the structural equivalences.

Both run without hardware:
```
python3 -m unittest test_gen_upsample_taps      # 8 tests
gcc -O2 -Isrc/dsp -o /tmp/tu tools/dsp/test_upsample.c src/dsp/csd_upsample.c -lm && /tmp/tu
```

## 8. Open items

1. **15 kHz corner**: removes programme content from 15-21.6 kHz on 48 kHz
   material. If noise shaping was the intent, this belongs after the mixer or in
   the shaper, not in the programme path.
2. **Halfband length vs image rejection**: 127 taps gives ~61 dB measured; the
   cost curve needs a measured sweep with the C implementation, not a model.
3. **Not wired into `csd_dsp.xc`**; no XS3 build, no cycle count, no hardware
   measurement of THD or noise.
4. **Rate switching** is not plumbed: an unsupported rate emits nothing rather
   than a wrong-rate signal, and the caller must reset the cascade on rate change.
