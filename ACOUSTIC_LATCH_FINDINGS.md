# Acoustic channel-collapse latch (measured 2026-11-05)

Measured on the known-good release image `2026.11.01.85-346fe14` (loaded via
`rig/load_xe.sh`, device `*IDN?` = `ClearSound,CSD-22,201b0d69,2026.11.01.85-346fe14`,
local .xe md5 `76423b684ebee3426a1a0d0681c65305`). It reproduces on that image, so it is
NOT a regression from the bit-perfect/tap work and must not be bisected against it.

## Symptom

After a specific stream sequence, the device latches into a state where:

- a LEFT-channel input produces **no acoustic output at all**
- a RIGHT-channel input is routed to **both** DAC outputs

Bench mic (card 5, ADAP headset, S24_3LE/48k) at 1 kHz, dBFS:

| state | L drive | R drive | R-drive corr(L,R) |
|---|---|---|---|
| working | micL -32.5 / micR -75.2 | micR -31.1 / micL -73.0 | -0.07 |
| degraded | micL -100.3 (repeat -105.3) / micR -98.8 (-97.1) | micL -31.5 / micR -30.7 | **0.9998** |

Isolation collapses from 42 dB to +0.8 dB.

## Why it is inside the DUT

1. **Mic left capsule is alive while degraded**: it records the R-drive tone at -31.5 dBFS.
   During silence both capsules are balanced (rms -53.4 vs -52.8, 0.6 dB apart, non-identical,
   r=0.906), so it is not a capture-path or duplicate-channel artifact.
2. **Left output is capable of sound**: it reproduces the right-channel tone at full level.
3. **DUT builtin mic (`hw:3,0` S32_LE, independent capture path)**: L-drive -144.6/-147.0 dBFS
   (noise floor, no tone) vs R-drive -113.1 dBFS, which is +6 dB over the single-output working
   level = coherent sum of two radiating outputs.
4. **Host sees nothing**: every ALSA control nominal (both `Mix 2 Playback Volume` stages 192,
   all switches on), `DIAG:DSP:PRESet?` = 0, and `.85` answers -113 `Undefined header` for
   `VOLume:TRIM:LEFT?`, `VOLume:TRIM:RIGHT?`, `OUTPut:STATe?`, `MULt?`, `*TST?`,
   `POWER:TIMEout?`, `DEBUG:LOOPback?` (those headers post-date `.85`). The latch is not
   expressed on any host-visible surface, so only acoustic measurement detects it.

## Reproduction

Deterministic, 2/2 attempts, first pass of the recipe:

1. `rig/csd_suite.py --only 08` -> PASS (+42.5 / +42.0 dB isolation)
2. run the standalone FFT probe (`aplay -t raw -f S32_LE` into `hw:3,0`, one channel driven)
   -> degraded state appears

It did not self-clear across ~15 min of SCPI reads, `amixer` control resets, host pink noise,
or repeated wake/play streams.

## Recovery granularity (all three legs measured)

- `pkill -x xgdbserver` alone: **no recovery** - the `.xe` keeps running (card still enumerates,
  `*IDN?` answers) and the degraded numbers persist bit-identically.
- `pkill -x xrun` alone: **no recovery** - board becomes unresponsive (`*IDN?` timeout, USB
  re-enumerates 044 -> 045), both drives at floor.
- both kills **plus** `xrun --io --xscope` image reload (`rig/load_xe.sh`): **recovered**
  (L-drive -37.7 / -80.4, R-drive -79.9 / -37.7, corr 0.036), then case 08 PASS.

## Open questions for the fix

- What in step 2 latches it: the S32_LE raw stream, the sample-rate/format transition away from
  the suite's configured rate, or a channel-count/`chmap` edge?
- Which block holds the latch: codec routing, the mixer's channel mapping, or the output mux.
- Whether it can be made visible to the host at all; today a green-looking rig can be measuring
  a dead left channel, which is the dangerous property of this bug.

## Standing fixture notes learned the hard way

- `numid` numbers are firmware-shape dependent. On `.85`, card 3 `numid=5` is the boolean
  `Mix 2 Playback Switch`, not a volume. Blind `amixer cset numid=...` pokes silently set the
  wrong control and produced a false "no 1 kHz tone at all" report. Always enumerate
  `amixer -c <card> controls` first and address by name.
- The bench mic S24_3LE decode must take bytes 0-2 as L and bytes 3-5 as R. A
  `np.stack([v, v])` duplicate-channel decode was written once and hid a real result.
- The ALC897 analog output (`hw:2,0`) has both Line Out and Front Headphone jack sensors off,
  so it is not usable as a DUT-independent acoustic stimulus on this rig.
