# Upsampling to 384 kHz -- design record

Branch `upsampling`. Scaffold only: the DSP core and its tables are complete and
tested off-target; nothing is wired into `csd_dsp.xc` yet, and nothing has run on
hardware.

## Requirement

48, 96 and 192 kHz inputs are converted to 384 kHz with **sinc** interpolation,
not linear interpolation. A second, linear-phase low pass starts rolling off
around 15 kHz and is equivalent to at least a 4th-order filter, with minimum
group-delay dispersion.

## Why sinc and not linear interpolation

Linear interpolation is a 2-tap triangular kernel. Its first image is only about
-13 dB down; the scaffold's tests measure the sinc design's image at **-140 dB**
relative to DC (spec was -80 dB). Both numbers come from the shipped fixed-point
tables, not from an ideal filter.

## Stage A -- polyphase sinc interpolator

Integer ratios only: 48 -> 8x, 96 -> 4x, 192 -> 2x, all landing exactly on
384 kHz. One windowed-sinc prototype per ratio, Kaiser window, 80 dB design
attenuation, 64 taps per phase:

| input | ratio | prototype taps | taps/phase | MAC/s per channel |
|-------|-------|----------------|------------|-------------------|
| 48 kHz  | 8 | 512 | 64 | 24.6 M |
| 96 kHz  | 4 | 256 | 64 | 24.6 M |
| 192 kHz | 2 | 128 | 64 | 24.6 M |

The prototype is normalised to sum to **L**, not 1. Each output sample is
produced by one phase, so a prototype summing to 1 would put DC out at 1/L
(-18 dB at 8x). This was caught by the host test's DC check, not by inspection.

Passband edge is 0.45 x fs_in, so 48 kHz keeps to 21.6 kHz; the stopband starts
at fs_in - f_pass (26.4 kHz), which is where the first image lives.

## Stage B -- band limit / noise shaping

Magnitude follows a 4th-order Butterworth with a 15 kHz corner; it is realised as
a **511-tap symmetric FIR** at 384 kHz, so phase is exactly linear and group
delay is flat at 255 samples (0.66 ms). A real 4th-order IIR Butterworth has
frequency-dependent group delay, which is exactly what the "minimum dispersion"
requirement excludes.

Measured against the ideal curve: 0 dB at DC, -3 dB at 15 kHz, -48 dB at 60 kHz,
all within 1.5 dB. The response is monotone, so there is no ripple-induced
dispersion.

**Trade-off to decide before hardware:** a 15 kHz corner removes programme
content from 15-21.6 kHz on 48 kHz material. That is what was specified, but it
is audible on cymbals and strings; if the intent was noise shaping only, the
corner belongs in the noise-shaping stage and not in the programme path.

## Cost

Per stereo pair at 48 kHz input: 2 x 24.6 M (stage A) + 2 x 196.2 M (stage B) =
**441 M MAC/s**. Stage B dominates because it runs at 384 kHz with 511 taps. On
an 800 MHz XS3 tile that is roughly half of one core before loop overhead, so
the open decision is whether stage B stays a long FIR or becomes a shorter FIR
plus a decimated noise shaper.


## Cost reduction by symmetry

**Stage B: folded, halved.** The 511-tap kernel is symmetric, `c[k] == c[TAPS-1-k]`,
so each multiply serves two taps:

```
acc += c[k] * (hist[pos-k] + hist[pos-(TAPS-1-k)])    k = 0 .. 254
acc += c[255] * hist[pos-255]                          centre tap
```

511 multiplies -> 256, and one coefficient load instead of two. The host test
compares every output against the unfolded convolution: **0 mismatches of 4096**.
Folding changes the work, never the result.

**Stage A: folding is not available, and here is why.** A symmetric prototype
does not decompose into symmetric phases. The partner of tap `k*L+j` is tap
`N-1-(k*L+j)`, whose phase is `(L-1-j) mod L` -- a *different* output sample. The
folded product is a single multiply that would have to be added into two
different accumulators, and splitting it costs the two multiplies it saved. It
only folds when every pair lands in one phase, i.e. `N-1 = 0 (mod L)`, which is
impossible for L >= 2. Computing phase sums and differences instead is also a
wash: `(a+b)` and `(a-b)` need two multiplies for the same two outputs.

The real saving in stage A is structural, not arithmetic: **cascade of 2x
halfband stages** (48 kHz -> 96 -> 192 -> 384). A halfband has every other tap
zero and a symmetric remainder, so a 2x stage costs about N/4 multiplies per
output, i.e. roughly half of the equivalent polyphase prototype -- and the
cascade uses shorter filters. That is the next change, not part of this commit.


### Stage A with structural zeros -- 2x halfband cascade

A windowed sinc has **no** exact zeros: the shipped stage-A tables contain 0 zero
coefficients out of 512, 256 and 128 taps, so a zero-skip test there is dead code.
The filter that does have zeros is the halfband. Its kernel is 127 taps with
**62 exact zeros** at every other offset; only the odd-indexed taps are nonzero,
plus the centre.

Phase assignment is easy to get backwards, and the test caught both mistakes:

- the centre tap sits at index 63 (odd), so the **identity branch lands on the
  ODD output samples** and the interpolated phase on the even ones;
- the test's naive reference must start where `x[n-63]` is valid, not at n=0.

Cost per input sample per 2x stage: **32 multiplies** (32 symmetric pairs over
the 64 even-indexed taps, zeros never loaded) plus a free identity branch,
against 127 for the generic polyphase 2x. For 48 -> 384 kHz that is three stages
at 48/96/192 kHz in, i.e. 32 x (48k + 96k + 192k) = 10.7 M MAC/s per channel,
against 24.6 M for the 512-tap 8x prototype: **2.3x less**, with the same
linear-phase property.

Proof: identity branch matches the input bit for bit (0 of 512), interpolated
phase matches the naive 64-tap even-index sum bit for bit (0 of 448), and the
band-limit folding is still 0 of 4096 against the unfolded convolution.

## Files

- `tools/dsp/gen_upsample_taps.py` -- designs both stages, emits Q30 tables.
- `src/dsp/csd_dsp_taps.h` -- generated tables; do not edit by hand.
- `src/dsp/csd_upsample.[ch]` -- portable fixed-point core, no allocation, no
  XMOS dependency, so it can be a C static on a tile.
- `tools/dsp/test_gen_upsample_taps.py` -- 8 property tests on the quantised
  coefficients (DC gain, symmetry -> linear phase, image rejection, passband
  flatness, 4th-order tracking, monotonicity).
- `tools/dsp/test_upsample.c` -- 5 host checks on the runtime, compiled with gcc.

## Proof

```
python3 -m unittest test_gen_upsample_taps   -> Ran 8 tests ... OK
gcc ... test_upsample.c csd_upsample.c -lm    -> RESULT: PASS
   DC gain 1.0000 | impulse peak 319 (expect 319) | 1 kHz -12.04 dBFS
   image at 47 kHz -140.9 dB | band-limit DC 1.000
```

The 319-sample impulse position is the linear-phase delay: (512-1)/2 = 255.5 at
384 kHz, sampled at phase 0, plus the 8-sample input offset.

## Not done

- Not wired into `csd_dsp.xc`; no XMOS build yet, so no cycle count on target.
- Rate switching between 48/96/192 is not plumbed; `csd_up_ratio()` returns 0 for
  anything else and the core emits nothing rather than guessing.
- Stage B corner (15 kHz) and length (511) are design choices pending sign-off.
