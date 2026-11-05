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

## Register investigation (2026-11-05, same image) - codec CONFIG hypothesis killed

Two source reads proposed ES9219Q reg `0x02` `MIXING_N_AUTOMUTE_CONFIG` with
`CH1_MIX_SEL=01` (routes channel-2 data to DAC1) as the latch, which fits the symptom on
paper. Measured on the latched board, it is wrong.

Working baseline (`REGister:READ?`, base64 path):
`0x01=8C 0x02=34 0x03=40 0x07=00 0x0A=02 0x0D=00 0x0F=00 0x10=00 0x1B=C4 0x1D=00 0x40=89 0x41=01 0x48=02 0x4D=03 0x86=C0`

Latched state, same reads:
`0x01=8C 0x02=34 0x03=40 0x07=00 0x0A=02 0x0D=00 0x0F=00 0x10=00 0x1B=C4 0x1D=00 0x40=88 0x41=00 0x48=02 0x4D=02 0x86=C0`

**Exactly three deltas, all clock/status bits:**

| reg | working | latched | meaning |
|---|---|---|---|
| `0x40` CHIP_STATUS | `0x89` | `0x88` | clock-lock bit0 lost |
| `0x41` | `0x01` | `0x00` | status lost |
| `0x4D` LOCK_STATUS | `0x03` | `0x02` | lock bit0 lost |

Every routing suspect is unchanged: `0x01=0x8C`, `0x02=0x34` with `CH1_MIX_SEL=00` (the
hypothesised `0x05`/`0x01` was never present), `0x0D=0x00` mono-clear, and `0x0F/0x10=0x00`
in both states, so there is no attenuation delta either.

Single-write discriminator while latched, re-measuring separation after each write:

| write | readback | L-only (micL/micR) | R-only | corr |
|---|---|---|---|---|
| `0x02=0x34` | `0x34` | -102.1 / -94.0 | -26.5 / -25.6 | +0.9990 |
| `0x0D=0x00` | `0x00` | -94.2 / -93.8 | -26.6 / -25.7 | +0.9989 |
| `0x01=0x8C` | `0x8C` | -98.3 / -92.8 | -26.3 / -25.5 | +0.9990 |
| `0xC0=0x01` soft reset | `0x00` | -95.0 / -90.8 | -96.7 / -94.5 | chip dead, reload required |

No routing write restored separation. The soft reset proves the read path reflects live
silicon (registers visibly move), so the register equality is genuine and not shadow RAM.

**Where that leaves the mechanism.** A codec that cannot resolve LRCLK framing cannot
distinguish slot 1 from slot 2: a left-only stream puts zero into both outputs, a right-only
stream puts the same word into both, coherently at +6 dB - which is exactly the measured
asymmetry, without any register being misconfigured. The fault is therefore in the I2S
clock/framing interface feeding the codec, upstream of every readable register.

Complication to resolve, not hand-wave: the DUT's own builtin ADC path (`hw:3,0`, same chip)
still returns a usable 1 kHz tone while latched (-113 dBFS on R-drive, vs -119 dBFS working),
so some clocking survives. Either lock bit0 is not what it appears, or the ADC and DAC sides
differ in how they derive framing.

Corrections to assumptions held earlier in this investigation:

- boot `0x02` is `0x34`, **not** `0x04` (bits 5:4 set - automute config region)
- boot `0x0D` is `0x40`; `0x00` is the post-`AudioHwConfig` value, so "AudioHwInit writes
  `0x0D=0x00`" was wrong
- `.85` build shape is `PCM_DSP_EN=1 CSD_DIAG_MIDI=1 CSD_DSP_PRESET_DEFAULT=2 CSD_GEN_EN=1
  CSD_GEN_INSTRUMENT MIXER=1 MAX_MIX_COUNT=2` (`release/RELEASE-2026.11.01.85.md:19-20`), so
  the mixer and its boot-once tables ARE live - reading defaults out of `xua_defs.h` gives
  `MIXER=0` and is not the release shape
- `POWER:TIMEout?` is rejected on `.85`: `-113,'Undefined header'`

Board left: recovered by image reload, case 08 PASS -
`L-only: micL -22.1 micR -65.0 (isolation +42.8 dB) | R-only: micL -63.0 micR -21.0 (isolation +42.0 dB)`.

## Second episode: the latch became unrecoverable, and there is no software POR path

After the register discriminator above (which included `REGister:WRITE 0xC0 0x01`, the codec
soft reset the firmware itself never uses), the board no longer recovers by any software means.

Recovery attempts, all measured, all failed, all while the fault was acoustically re-confirmed:

| attempt | result |
|---|---|
| `rig/load_xe.sh` reload of the same .xe, stamp PROVEN | still latched (L-only micL -101.0 / micR -89.9; R-only -32.2 / -31.3, corr +0.9967) |
| `pkill -x xrun` AND `pkill -x xgdbserver` + reload | no change |
| `USBDEVFS_RESET` ioctl, re-enumeration, `*IDN?` correct | no change (-97.0 / -89.7; -32.2 / -31.3, corr +0.9981) |
| format variation S16_LE / S24_3LE / S32_LE, L-only | all silent (-92.7 / -93.4 / -97.6) |
| 96000 -> 48000 rate round trip (fresh `AudioHWConfig`) | no change |
| idle power-down + wake (full codec re-init, `0x07` verified) | no change |
| second clean reload booting in PLL mode (`CHIPID tries: 0 (pll_bypass=0)`) | still latched (L-only -95.3 / -93.0) |

Final state was left latched, case 08 **FAIL**, verbatim:
`L-only: micL -99.3 micR -93.4 (isolation -5.9 dB) | R-only: micL -22.0 micR -21.2 (isolation +0.8 dB)`
`PASS=0 FAIL=1` - and no PASS line was printed for case 08 in that session.

### Why reload used to work and now does not - firmware gap

The earlier episode recovered with `pkill xrun` + `pkill xgdbserver` + reload. This one does
not, and the difference is consistent with a real defect in the driver:

- **nothing in the firmware ever ASSERTS the codec reset GPIO.** `csd_AudioHwInit` only
  *releases* it (`board.xc:993`, `GPIO_WR(0, CODEC_RELEASE_RESET)`); the reset line is never
  driven low by any code path.
- the driver has **zero write sites for `0xC0` SOFT_RESET** (`es9219q.h:33`), so the chip is
  never reset by firmware either.

Consequence: if the codec is left in a non-power-on internal state by any means - here, a
diagnostic `0xC0` write - an image reload cannot recover it, because reload only re-releases a
reset line that was never asserted. **A reload is therefore not a codec POR, and no software
path on this build can produce one.** Recovery requires a physical power cycle of the DUT.

Fixes this gap justifies, independent of the latch's root cause:

1. assert then release the codec reset GPIO at the top of `csd_AudioHwInit`, so every image
   load starts from a genuine codec power-on state and a stuck chip cannot survive a reload.
2. read back and verify the boot register set instead of printing a 61-register dump that is
   compared against nothing.
3. treat `REGister:WRITE` to codec-internal control registers (`0xC0` in particular) as
   destructive: gate it, or at minimum require an explicit unlock, because it can put the board
   into a state only a power cycle escapes.

### Two distinct states, do not conflate them

- **State A - the original latch**: L input dropped, R on both outputs; *recoverable* by
  killing both loader processes and reloading. Reproduced 2/2 deterministically.
- **State B - post-`0xC0` soft reset**: all registers `0x00`, both outputs silent, survives
  reload, USB reset, rate change, format change and codec re-init. Needs physical power.

Clock evidence obtained while latched (register/status only - **this rig has no scope or logic
analyzer, so no pin-level BCLK/WS/MCLK measurement was made**): `0x40=0x89 0x41=0x01 0x4D=0x03
0x48=0x02 0x0A=0x02 0x1D=0x00`, `amixer -c 3 cget numid=8` -> `values=on`, firmware prints
`MCLK: 49152000` and `PLL_LOCK: 1` in both states. The previously recorded `0x40=0x88 /
0x41=0x00 / 0x4D=0x02` deltas did **not** reproduce in this episode; a residual `0x4D=0x01`
(vs `0x03`) appeared later after an idle power-down/wake and never returned.

Boot stream-open sequences are byte-identical between working and latched sessions
(`receive_command: 30 / aud stream start / AudioHWConfig:Mute / AudioHWConfig:F:48000
MCK:49152000 DAC:16,ADC:24 / ... / AudioHWConfig:UnMute`), and the firmware emits no BCLK-divide
or port-config print at all, so nothing in the log distinguishes the trigger from the baseline.

### Rig instruments that must not be trusted as evidence

- `rig/mixer_mirror_probe.py` **fails its own self-check even on a fresh reload**
  (`FAIL SELF-CHECK: mirror carries a real tone (silence above is meaningful) [peak -55.0 dBFS]`),
  so a dead mirror is an instrument fault, not evidence about the xCORE data path. It needs
  fixing before the mirror can exonerate or convict anything.
- `.85` answers `-113,"Undefined header"` to `DEBUG:I2S?`, `POWER:TIMEout?`, and to a single
  `REGister:READ?` sweep of the whole 135-register map (per-address and 32-register chunks work).

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
