# I2S hardware-loopback hook (pin 32 rework)

## The idea

Instead of copying transmitted samples into a RAM array and substituting them back
into the receive stream (what `csd_mb_loop_take()` does today, one copy per sample
in the audio thread), jump the DAC I2S data line to the **spare second ADC data
input** on the xCORE. The core then clocks a word out and reads the same word back
from a pin, phase-locked to its own BCLK/LRCLK. Zero copies, zero per-sample cost.

Why this matters: at 600 MHz the copy-based tap cannot service every frame —
measured 771,072 zero words in 11.5 M frames, coverage 0.9331, 2204 slips — while
the audio itself is clean at that clock (case 08 PASS, 42.0/42.4 dB isolation).
The bit-perfect failure at 600 MHz is instrumentation cost, not audio.

## Board fact (from the schematic)

| net | pin | xCORE pin name | port |
|---|---|---|---|
| DATA ADC0 → I2S ADC DATA 0 | 53 | X1D34/1K | `XS1_PORT_1K` |
| **DATA ADC1 → I2S ADC DATA 1** | **32** | **X1D13/1F** | **`XS1_PORT_1F`** (spare) |
| DATA DAC0 → I2S DAC DATA 0 | 14 | X1D11/1D | `XS1_PORT_1C` |
| LRCLK | 10 | X1D01/1B | `XS1_PORT_1B` |
| BCLK | 9 | X1D00/1A | `XS1_PORT_1A` |

Rework: wire pin 14 (`DATA DAC0`) to pin 32 (`DATA ADC1`). Optionally add a series
resistor / test point, but the xCORE drives pin 14 and reads pin 32, so a direct
link is what the test needs.

## Why NOT `I2S_WIRES_ADC=2`

`lib_xua/src/core/main.xc:77-96` declares `p_i2s_adc[I2S_WIRES_ADC]` and picks up
`PORT_I2S_ADC1` when the count is 2. Tempting, but wrong for this: an extra ADC
**wire** means an extra pair of audio channels, which changes the input channel
count and therefore the USB descriptor and the host-visible mic stream. The
loopback pin must be a dedicated port read at the tap site, not a second ADC wire.

## The hook, in order

1. `src/ClearSoundDac.xn`, tile 1 (next to the other I2S ports, around line 70):
   `<Port Location="XS1_PORT_1F" Name="PORT_I2S_ADC_DATA_1"/>`
2. `src/core/xua_defs.h`, beside `PORT_I2S_ADC0` (line 23):
   `#define PORT_I2S_ADC1 PORT_I2S_ADC_DATA_1`
   and declare it where the audiohub can see it, on tile 1, clocked from BCLK —
   the same clock the ADC data port uses, so the read is framed identically:
   `on tile[XUA_AUDIO_IO_TILE_NUM] : buffered in port:32 p_i2s_adc_lb = PORT_I2S_ADC1;`
   then `set_port_clock(p_i2s_adc_lb, p_i2s_bclk);`
3. `lib_xua/src/core/audiohub/xua_audiohub.xc`, in the receive half-frame where
   `csd_mb_loop_take()` is called: when the loopback is armed, take the word from
   `p_i2s_adc_lb :> word` instead of from `csd_mb_loop_tx[]`. Keep the RAM path as
   the fallback for boards without the rework, selected by a define (for example
   `CSD_LOOPBACK_FROM_PIN`), so one source tree serves both builds.
4. The MIDI arming command (`DEBUG:LOOPback 1`) stays exactly as it is — the owner's
   rule that only a MIDI command can enable the tap does not change.

## Dependency pin (lib_xua)

`lib_xua` is a **separate git repository** here, not a gitlink inside the app tree, so the
app cannot record it by submodule. The app builds against:

- repo: `/home/ycui/sw-audio-sc/lib_xua` (branch `csd-121005`, was detached)
- commit: **`77245f99`** - "audiohub: guard the I2S monitor call behind CSD_MB_I2S_MONITOR"
- its parent, the tap hooks: `021c97ae` (cherry-pick of `fefc9a1c`)

Any commit in the app repo that changes behaviour must therefore ALSO commit the
`lib_xua` tree and state the SHA here, otherwise the "whole dependency tree" is not
recorded and a rebuild is not reproducible. The release note for `2026.11.01.85` shows the
same pattern (it pins `820fab5` and calls out the drift).

## What to prove after the rework

- `DEBUG:LOOPback?` frame counter still advances while armed (the pin is delivering).
- Gate coverage 1.0000 with zero-word 0 and slips 0 **at 600 MHz**.
- Case 06 PASS with 0 differing bits at 600 MHz; then re-measure 800 MHz for no
  regression.
- If the pin read returns a constant or all zeros, the wire is not connected where
  the .xn says it is — verify pin 32 against the package ball map before touching
  firmware again.
