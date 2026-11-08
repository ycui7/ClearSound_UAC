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

Kernel: **161 taps** (N = 4m+1, so the centre index 80 is EVEN and the delay is
the whole number 40 samples). Nonzero taps sit at the **odd** indices; the
**even** indices are 80 exact zeros, apart from the centre. That is the property that saves the work: zeros are never loaded.

Phase mapping (this is the part that is easy to get backwards -- the centre index
63 is ODD, so the identity branch lands on the odd output samples):

```
  x[n] ──┬────────────────────────────────────────▶ y[2n]   = x[n-40]     (identity, delay 40)
         │
         └──▶ 40 symmetric pairs over h[2k+1] ─────▶ y[2n+1]                (interpolated)
              sum q=0..39  h[2q+1] * (x[n-q] + x[n-79+q])
```

Cost per input sample per stage: **40 multiplies + 1 delay**, against 161 for a
generic polyphase 2x. The kernel is designed to **120 dB** stopband attenuation
(Kaiser beta 12.27). The interpolated taps are normalised so they sum to 1.0,
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
| 48 kHz  | 40 x (48k+96k+192k) = 13.4 M | 256 x 384k = 98.3 M | 112 M |
| 96 kHz  | 40 x (96k+192k) = 11.5 M     | 98.3 M              | 110 M |
| 192 kHz | 40 x 192k = 7.7 M            | 98.3 M              | 106 M |

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

**Image rejection: -154.6 dB measured** through the three-stage 48 kHz path with a
9375 Hz tone (bin 100 of a 4096-point Goertzel at 384 kHz). Every spur at
k*48k +/- 9.375 kHz sits below -120 dB, so the 120 dB requirement is met with
margin.

Earlier numbers of -60.6 dB for this same path were **measurement error**, not
filter behaviour: 10 kHz is 106.67 bins at 384 kHz, so the Goertzel leakage floor
masqueraded as the image. The give-away was that the figure did not move when the
kernel changed from 127 taps / 80 dB to 161 taps / 120 dB. Always bin-centre the
tone.

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


## Scaffold (XC integration) -- 2026-11-07

The upsampler is the DSP in this build. It **replaces** the stream; it never
adds to it. Exactly one stream per channel reaches the mixer.

### Modules

| File | Language | Role |
|---|---|---|
| `src/dsp/csd_upsample.c` / `.h` | C | filter core: halfband cascade, band limit, polyphase reference |
| `src/dsp/csd_dsp_taps.h` | C | generated Q30 coefficient tables |
| `src/dsp/csd_ups_channel.c` | C | per-frame glue, delivery queues, arming state |
| `src/extensions/csd_upsample_hook.h` | C/XC | public prototypes, callable from XC |
| `src/extensions/csd_upsample.h` | C/XC | forwarding header: `src/dsp` is globbed for sources but is not on the XC include path |
| `src/extensions/csd_dsp.xc` | XC | the seam, in the audio thread |

### The seam

In the I2S thread, where `csd_dsp_frame()` used to be called unconditionally:

```c
#ifdef CSD_UPSAMPLE_EN
    if (!csd_ups_frame(sampsFromUsbToAudio)) {
        csd_dsp_frame(sampsFromUsbToAudio);
    }
#else
    csd_dsp_frame(sampsFromUsbToAudio);
#endif
```

Armed, the upsampler owns the frame and the EQ path is bypassed. Disarmed, it
returns 0 without touching the frame and the normal DSP path runs. A codec that
is not at 384 kHz therefore falls back to known-good behaviour rather than
emitting words at the wrong rate. DoP detection runs before this, on the
host-rate frame, so a DSD stream still passes through byte-exact.

### Arming contract

`csd_ups_arm(out_hz)` is called from `csd_dsp_set_rate()` with `CSD_UPS_OUT_HZ`.
Only `384000` enables the path. This is a refusal, not a default: upsampled
words into a 48 kHz LRCLK are wrong by a factor of 8, which is not a degraded
result but a corrupt one.

### Build and qualify

```
bash tools/qualification/build_upsampler.sh      # clean XS3 build, upsampler on
bash tools/dsp/xc_sim/run_sim_qualification.sh   # XS3 simulator == tester math
```

### Boundary rules that the toolchain enforces

These are the four things that cost the most time, all of them compiler-checked:

1. `in` and `out` are **reserved in XC**. gcc accepts them, so the host suite
   stays green while the device build fails. Core parameters are `inbuf` /
   `outbuf`.
2. XC alias analysis rejects overlapping arguments (`scratch`, `scratch+4`) and
   same-object in/out arguments. Every scratch buffer is its own object.
3. A bare `*` is a **different pointer kind in C and in XC**, so a shared header
   cannot declare a function implemented in C and called from XC:
   `Type of symbol csd_ups_frame has mismatch with previous definition ... Did
   you mean to use *unsafe in the xc code to match the * in C?` The header
   spells pointers through `CSD_UPS_CPTR`, which is `* unsafe` under `__XC__`.
4. Because of (3), the state structs must never appear in a signature an XC
   translation unit sees. That is why the per-frame glue is C
   (`csd_ups_channel.c`) rather than XC, and why the audio thread crosses into
   it through plain `int`/`unsigned` only.

Build flags come from a file, not an inline `-D` list: an inline list delivered
`-DCSD_PW_IDLE_MS=<value>"` with a literal trailing quote, so the macro expanded
to a string literal and produced parse errors at each use site. `od` on
`flags.make` confirmed it. The header default is the same number.

### Deferred by agreement

* **Filter kernel design.** The scaffold is fixed; the kernel (stage shapes,
  coefficients, the 15 kHz roll-off policy in the programme path) is a separate
  discussion.
* **Hardware clocking.** The codec and I2S must be configured at 384 kHz
  independently of the USB stream rate. That is an audiohub/lib_xua matter, not
  a frame-hook matter. Until it is done the path stays disarmed, by design.
* **Headroom.** The core saturates at int32. A full-scale random sequence
  legitimately overshoots the halfband, so full-scale programme material can
  clip. Decide between headroom and an explicit clip policy with the kernel.


## Rate families -- 48 series and 44.1 series

Two families, one kernel. `csd_up_out_fs()` is the single source of truth.

| Input | Output | Ratio | Cascade stages |
|---|---|---|---|
| 48 000 Hz | 384 000 Hz | 8 | 3 |
| 96 000 Hz | 384 000 Hz | 4 | 2 |
| 192 000 Hz | 384 000 Hz | 2 | 1 |
| 44 100 Hz | 352 800 Hz | 8 | 3 |
| 88 200 Hz | 352 800 Hz | 4 | 2 |
| 176 400 Hz | 352 800 Hz | 2 | 1 |

**Only MCLK differs between the families.** The coefficient tables, the stage
count, the band limit and the group delay in samples are all identical. That is
structural, not a coincidence: everything is selected by *ratio*, never by rate
equality, because both families share the ratios 8, 4 and 2.

Consequences of that design, each one compiler- or test-enforced:

* `coef_for()` / `tpp_for()` pick the polyphase prototype by ratio. Picking by
  rate would silently deny the whole 44.1 kHz family a prototype.
* `csd_cascade_process()` picks its stage count from `csd_up_ratio()`. Rate
  equality would have left 44.1 kHz with no path at all.
* A rate in neither family returns ratio 0 and output rate 0, and the cascade
  leaves the output buffer **untouched** -- it does not write zeros. Approximate
  support for a rate that does not divide evenly would be a wrong answer, not a
  degraded one.
* `csd_ups_arm()` refuses a cross-family pairing: arming 44.1 kHz material at
  384 kHz would be a ratio of 8.708, so the path is disabled instead.
* `csd_ups_expected_out_hz(in_hz)` is what the board code should read to choose
  MCLK, so the mapping has exactly one home.

### What the identical taps mean at 352.8 kHz

Stated for the later kernel discussion, not as a defect:

* The halfband is normalised against its own output rate, so its image
  rejection tracks the rate automatically.
* The 511-tap band limit is a fixed coefficient set designed at 384 kHz with a
  15 kHz corner. At 352.8 kHz the same coefficients place the corner at
  15000 x 352.8/384 = 13.78 kHz. Accepted as-is (owner, 2026-11-07): the corner
  shift is not a problem to solve. One kernel serves both families.
* Group delay is 255 samples of the band limit either way: 0.664 ms at 384 kHz,
  0.723 ms at 352.8 kHz.

### Why the codec is not yet fed 384 kHz: the shared-MCLK coupling

Measured in source, not assumed. `lib_i2s/src/i2s_frame_master_impl.h:17`:

    configure_clock_src_divide(bclk, p_mclk, mclk_bclk_ratio >> 1);

The I2S bit clock is a fixed-ratio divider **of the MCLK pin**, and LRCLK follows
BCLK. Nothing calls `configure_clock_src` or sets a ClockBlock per rate: the only
thing that ties the codec's frame clock to the USB stream rate is the frequency
handed to `sw_pll_fixed_clock(mClk)` -- at `lib_xua/src/core/audiohub/xua_audiohub.xc:1026`
and again in `csd_AudioHwConfig` (`clearsounddac_board.xc:1769`), where
`mClk = samFreq * MCLK_MULTIPLIER` for the host's rate.

Consequence: raising MCLK 8x to feed the DAC 384 kHz raises LRCLK on every I2S
port, so the audio thread is paced 8x faster than the USB stream supplies samples.
The upsampler already closes that gap on the *data* side (it emits 8 frames per
input frame), but lib_xua has no way to say "port rate 384 kHz, stream rate
48 kHz" -- stream rate and port rate are one variable there. Separating them is
the enabling change, and it is a vendored-library change on the path that carries
the proven 24-bit/384 kHz bit-perfect result, so it is not made casually.

Until then the option is present, correct, and refuses -- which is the right
behaviour, because emitting 8x data into a 48 kHz LRCLK is wrong by a factor of
eight, not merely degraded.

## Persistence

The setting is journalled with the calibration trims in the same 32 B slot, at
record **version 3**: one checksum, one sequence, one validity gate, so the
option can never disagree with the trims after a partial write.

- The option word is the slot's trailing word, which sits *after* `chk`. For v3
  records the checksum is **extended to cover it**; v2 records keep their original
  six-word checksum and stay verifiable byte for byte. A setting that gates a
  hardware filter must not be the one unprotected field in the record.
- Migration is field-by-field against the record's own version, so a v2 board
  keeps its operator timeout and only gains `ups_en = 0`. Defaulting everything
  older than current would have thrown away a real setting.
- The value is adopted only from a record that passed magic, version, payload
  count, checksum and range gates. Erased flash never becomes boot behaviour.
- Two `_Static_assert`s pin the slot at 32 bytes and the option word at index 7.
  The checksum walks the struct as raw words and indexes word 7 directly, so
  member drift would silently stop protecting calibration -- a passing build would
  not show it.
- Only an explicit operator command journals. A rate-change re-grant persists
  nothing, or every rate change would append a NOR record and fill the
  128-slot sector.

## Runtime config option (default OFF)

One MIDI/SCPI control turns on XMOS upsampling and the ES9219Q internal FIR
bypass together:

```
F0 7E DIAG:UPSAMPLing:STATe 1 F7      # enable
F0 7E DIAG:UPSAMPLing:STATe? F7       # -> xmos_en,applied,frame_hz,codec_refusals,ups_refusals
```

They are one decision. 8x data with the chip FIR still on interpolates twice;
FIR bypassed without 8x data starves the modulators (datasheet v1.3 Table 3
p13, p22). Both failure modes are silence or noise, so the pair cannot be set
apart.

**Off by default**, which keeps the acoustically proven built-in filter.

**The interlock is the point of the design.** Bypass is granted only when the
frame clock the codec was actually configured with is 384000 or 352800. The
reply always reports resulting state, so a refusal is visible (`applied=0`,
refusal count rising) rather than looking like success. Reg 7 bit 3 is forced in
the tile[0] server's register-write path, so no other writer -- init, a
rate-change mute from tile[1], or a SCPI `REGister:WRITE` -- can undo it.

Clocks needed, and already generated: 384 kHz frame -> MCLK 49.152 MHz
(`MCLK_48`); 352.8 kHz frame -> 45.1584 MHz (`MCLK_441`). No 98 MHz clock.

Until the I2S output clock is decoupled from the USB stream rate in audiohub,
enabling the option with a 48 kHz stream is correctly refused.

## Verification

`tools/dsp/xc_sim/run_sim_qualification.sh` -- exit 0 only when the XS3
simulator report is byte-identical to the tester math. Currently 38 lines,
covering all six rates, both family mappings, exact-multiple ratios, refusal of
44 000 / 22 050 / 32 000 / 384 000 as inputs, and buffer-untouched behaviour on
refusal.


## Stage A depth: efficient minimal cascade (owner decision, 2026-11-07)

Depth follows the total ratio: 8x = 3 stages, 4x = 2, 2x = 1.

**Why it is forced.** A halfband's 62 structural zeros exist only at exactly 2x.
That is what makes stage A cheap -- 40 multiplies per input sample instead of
127 -- so every real stage must be a 2x, and the total ratio is 2^stages.

**Uniform depth was built, measured, and backed out.** Running three stages at
every rate means the short-rates carry padded stages, because integer stage
ratios can only be 2*2*2, 2*2*1 and 2*1*1. A ratio-1 stage copies: no filtering
and no added group delay, so all six rate hashes stayed bit-identical (48000
2255943590, 96000 2109678388, 192000 3038677792, 44100 1297205703, 88200
2227632825, 176400 765286489). It cost roughly two extra memcpys per frame at
192 kHz input and one at 96 kHz, and changed nothing audible or measurable. The
owner chose the efficient design; the padding is reverted.

**Rejected alternative, kept on record.** Non-integer stage ratios
(4 = 4/3 * 3/2 * 2) would give uniform depth without padding, but every stage
becomes an interpolate-then-decite resampler and the halfband zeros disappear --
losing the property that makes stage A cheap.

**If uniform depth is ever wanted again**, the shape that worked was a
`csd_up_stage(state, in, n, out, coef, ratio)` taking its coefficient pointer
from a per-rate plan, with the cascade as one loop of fixed length. That seam is
what made the experiment behaviour-neutral in the first place.
