# Removal: per-call upsampler engine (2026-11-10)

Owner directive, verbatim: *"from now on, when we refer to upsampler, we refer to the dsp
implementation. remove the old per function call upsampler from the code base. and do not
mention the old sampler again. it is wrong implementation."*

So: **upsampler = the DSP block engine** (`lib_audio_dsp` `fd_block_fir`, AN02027, reached
through the `csd_dsp_fd.c` adapter shape). The per-`UserBufferManagement`-call interpolator
(`csd_ups_host_pair` -> half-band cascade) is the wrong implementation and comes out.

## Why this is a refactor, not an `rm`

Three surviving files use the dead engine for things that are **not filtering** and must
survive it:

| dependency | site | must be preserved as |
|---|---|---|
| fabric/wire rate forcing | `clearsounddac_board.xc:1867-1880` (`csd_ups_set_want(DAC_TRIM_UPS_GET())`, `csd_ups_expected_out_hz(samFreq)`), `:1965-1984` | the decision "host rate -> forced family wire rate + `xua_csd_set_usb_ratio`", fed by the DSP engine's ratio |
| persisted enable option | `clearsounddac_board.xc:908-930` (`AUDIOHW_CMD_TRIM_UPS_GET/SET`) | same persisted trim, now meaning "DSP engine on/off" |
| MIDI/SCPI control surface | `csd_diag_scpi.c` `DIAG:UPSAMPLing:*` (`STATe`, `PHASE`, `DIVider?`, `INFO?`) | same command names, re-pointed at the DSP engine |
| XC timer shim | `clearsounddac_board.xc:1822-1825` (`csd_ups_now`, app C cannot read a timer) | keep while any C-side engine needs timestamps |
| mic decimator (port) | `lib_xua/src/core/audiohub/xua_audiohub.xc:614`, `:780` | **untouched** — separate, verified working, no mul/div in the per-word path |

## Delete

- `src/dsp/csd_hb_hot.xc` (10.7 KB) — half-band cascade, `hb_run` MAC loop
- `src/dsp/csd_ups_channel.c` (20.8 KB) — host-pair fill, `s_q[2][8]` ring, hold stub
- `src/dsp/csd_upsample.c`, `src/dsp/csd_upsample.h`, `src/extensions/csd_upsample.h`
- `src/extensions/csd_upsample_hook.h`
- `src/dsp/csd_dsp_taps.h` — referenced ONLY by the three files above, so it dies with them
- `tools/dsp/test_upsample.c`, `tools/dsp/gen_upsample_taps.py`,
  `tools/dsp/xc_sim/csd_ups_sim.xc`, `tools/dsp/xc_sim/sim_main.xc`
- `build/` is glob-driven; regenerate, do not hand-edit

## Edit

1. `src/extensions/csd_dsp.xc` — drop `#include "csd_upsample_hook.h"` (:14); delete the
   `CSD_UPSAMPLE_EN` block at :242-263 (`csd_ups_note_set_rate_xc`, `csd_ups_set_input_rate`,
   `csd_ups_expected_out_hz`, `csd_ups_arm`) and the fill block at :694-710
   (`csd_ups_host_active`, `csd_ups_host_pair`). The rate-change path keeps calling the DSP
   engine's `config(rate_idx)`.
2. `src/extensions/clearsounddac_board.xc` — same treatment at :37-38, :908-930, :1371-1375,
   :1822-1825, :1847-1880, :1965-1984. Replace engine queries with the DSP engine's ratio
   source; keep the trim storage and the forced-divider write.
3. `src/extensions/csd_diag_scpi.c` — keep `DIAG:UPSAMPLing:*` names, back them with the DSP
   engine; drop fields that only the deleted engine produced.
4. `lib_xua` — no changes.

## Verify, in order

1. build clean (`tools/qualification/build_upsampler.sh`, toolchain `/home/ycui/XMOS-15.3.1/`)
2. flash, `*IDN?` stamp
3. mic decimator probe (`~/.hermes/cache/scratch/csd_decim_probe.py`): N = 8/4/2/1 at
   48/96/192/384 kHz
4. bit-perfect gate disarmed at 384 kHz: expect 24/24 cells, 100.0000%
5. acoustic sweep (`~/.hermes/cache/scratch/csd_acoustic_rates.py`): bypass vs engine at
   48/96/192/384 kHz

## Open defect this removal must not bury

Acoustic sweep, build `2026.11.10.59`: 48/96/192 kHz agree to 0.2 dB, but at **384 kHz with
the DSP preset engaged** 1 kHz reads −81 dBFS (31 dB below its bypass level), 10 kHz −94 dBFS,
and L/R diverge by 27/38 dB. Asymmetry points at the 384 kHz coefficient table, not throughput.
Also: the shelf costs ~5 dB absolute gain where it should be flat outside the bass.
Discriminating probe: presets `0..7` at 384 kHz / 1 kHz — preset 1 is DELAY (plumbing, no
coefficients).
