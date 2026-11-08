# TEST.md — ClearSound DAC acceptance tests

This file describes every acceptance test in the ClearSound USB DAC rig, and
what each test requires. It is the only authoritative description of the case
list. The code is the source of truth. Where this file and the code disagree,
the code wins.

Sources:
- `csd-js-tester/rig/csd_suite.py` — the case list and the case logic.
- `csd-js-tester/rig/csd_bitperfect_gate.py` — the two bit-perfect gates.
- `csd-js-tester/rig/bp_score.py` — the bit-level scorer.
- `ACOUSTIC_LATCH_FINDINGS.md`, `I2S_LOOPBACK_HOOK.md`,
  `FLASH_AND_RECOVERY.md` — the premises and the measured traps.

The suite has 15 case ids: 01, 02, 03, 04, 05, 06, 07, 07.1, 08, 09, 10, 11,
12, 13, 14.

## 1. How to run

### Suite

Use the rig venv. The venv is
`/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester/.venv`
(Python 3.11, numpy, pyusb, python-rtmidi).

```
cd /home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester
PYTHONPATH=$PWD .venv/bin/python rig/csd_suite.py
```

The suite prints one line per case, then a SUMMARY block with
`PASS=n FAIL=n NOT-RUN=n TODO=n`.

Options:
- `--dac N` — pin the DAC ALSA card number. Default: auto-detect.
- `--mic N` — pin the bench microphone card number. Default: auto-detect.
- `--only 01,09` — run only the listed case ids. The baseline still runs.
- `--quick` — shorten each bit-perfect leg from 2.0 minutes to 0.5 minutes.
- `--allow-dfu` — open the gate on case 05. The case is still a TODO today.

### Single case

```
cd /home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester
PYTHONPATH=$PWD .venv/bin/python rig/csd_suite.py --only 06
```

`--only` does not skip the baseline. The suite still sets host volume,
zeroes both trims, sets DSP preset 0, disables power saving and disarms the
loopback tap before it runs the case.

### Bit-perfect gate, standalone

```
cd /home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester
PYTHONPATH=$PWD .venv/bin/python rig/csd_bitperfect_gate.py --minutes 5
```

- `--minutes N` — length of TEST 2. Default 5.
- `--skip-silence` — skip TEST 1. The leg prints `SKIP(void)`.
- `--skip-noise` — skip TEST 2. The leg prints `SKIP(void)`.
- Card: environment variable `CSD_CARD`, default `3`.
- Rate: environment variable `CSD_RATE`, default `384000`.

The gate exits 0 when both legs pass. It also exits 0 when a leg did not run.
The suite therefore never reads the exit code. It reads the `GATE:` line only.

### Exit status of the suite

- 0 — every measurable case passed. TODO items may remain.
- 1 — at least one case FAILED, or at least one case was NOT-RUN.
- 2 — fatal fixture error: no DAC card in `/proc/asound/cards`, or `--only`
  matched no case.

## 2. Result semantics

- **PASS** — the measurement ran and met the criterion. The number is printed
  with the verdict.
- **FAIL** — the measurement ran and did not meet the criterion. Firmware or
  hardware is implicated. The printed number is the evidence.
- **NOT-RUN** — the measurement could not be made. Examples: the probe is deaf,
  the board is absent, the control is not writable, the transport does not
  answer. NOT-RUN indicts the fixture, never the firmware.
- **TODO** — the case cannot pass yet because a dependency does not exist. The
  detail field names the missing dependency. A TODO is **not** a pass. It never
  fails the run, and the summary counts it so it is not forgotten.
- **VOID / SKIP(void)** — a leg of the bit-perfect gate did not run. A leg that
  did not run prints `SKIP(void)`, never PASS. It is not evidence of anything.

The suite turns a harness exception into NOT-RUN with the text
`harness exception: ...`. A crash is a fixture fault, not a firmware verdict.

## 3. Cross-cutting premises

These premises apply to the whole run. The suite enforces them, or the gate
refuses to certify a result without them.

- **Host playback volume is at maximum on every control.** The suite sets every
  `Playback Volume` control to 192 and reads each one back. It anchors the
  read-back on `: values=`, because `amixer cget` prints `values=` twice and the
  capability line's value is the element count. A read-back more than 1 count
  away from the request is an error. A stale or reduced host volume crushes both
  channels equally and reads as a firmware regression.
- **DSP preset 0 (bypass) for case 06.** The gate asserts this before it
  streams. With a processing preset armed the tap reads processed words, so the
  comparison would measure the filter, not the wire. Preset 2 also emits a
  fixed-point limit cycle on silence (constant `0xFFFFFE` on L, `0xFFFFEA` on
  R), which breaks TEST 1 for a reason unrelated to leakage.
- **Generator off.** The gate sends `GENerator:OFF` and reads
  `GENerator:STATe?`. Only field 0 is the mode. The other eight fields are the
  wave descriptor and are non-zero by design. `0,48000,256,256,1,0,2,1,1` means
  OFF.
- **Mixer gain unity.** `MIXer:GAIN?` must read `32768,32768` (Q15, 32768 =
  unity). These are the mixer's per-source multipliers on the host stream. A
  value below unity scales every sample before the tap.
- **Power saving disabled for the whole run.** The baseline sends
  `POWER:TIMEout -1`, waits 2 s, and reads the value back. If the read-back is
  not `-1` the suite prints a WARNING and marks level and capture results
  unreliable. The original value is restored at exit through `atexit`. A tone is
  not used as a wake-up. A stream wakes the box as a side effect and would let a
  capture case measure a state the fixture itself caused.
- **The loopback tap is armed only by a MIDI command.** The only arming path is
  the SCPI row `DEBUG:LOOPback 1`. The suite disarms it at baseline
  (`DEBUG:LOOPback 0`), the gate disarms it on every return path, and the suite
  disarms it again before the first case. An armed tap substitutes its stored
  word into the RX stream, so every later capture case would otherwise measure
  the tap instead of the microphone. The tap is device state and survives the
  process that armed it.
- **DAC card matcher covers both build shapes.** The release image announces
  `ClearSound DAC (UAC2.0)` and its ALSA card label is `DACUAC20`. The debug
  image announces `XMOS AUDIO XMOS UAC2.0` and its label is `XMOSUAC20`. The
  suite tries `DACUAC20`, `XMOSUAC20`, `CLEARSOUND`, `SPECIALTYCIRCUITS`. It
  resolves cards from `/proc/asound/cards` on every call, because card numbers
  are assignment order and change between reboots.
- **The word `ears` is not a DAC token.** It is a substring of `Cl-ears-ound`,
  and it once made the rig record the DAC as its own microphone. The bench
  microphone tokens are `EARSGAIN`, `MINIDSP`, `USBAUDIOEARS`, `AUDIOADAP`,
  `ADAP`.
- **A stimulus that does not clear the capture floor by at least 6 dB reports
  NOT-RUN, never FAIL.** If the tone is not at least 6 dB above the measured
  floor, the probe cannot hear the device. A mute then looks silent and an
  isolation number is noise. The case returns NOT-RUN with the tone and floor
  levels printed. Cases 01, 09 and 10 apply this rule. Case 08 does not — see
  its limitations.
- **The capture floor is measured at 7–11 kHz.** That band is above the 1 kHz
  tone, above bench noise, and free of mains harmonics. An earlier version used
  bins 20–200, which is 10–100 Hz mains hum and room rumble. It read -16 dBFS,
  higher than the tone, so a working mute tested as "deaf".
- **Register reads are trusted only with a witness.** `REGister:READ?` always
  includes registers `0x03` and `0x07` in the same transaction. Boot init fixes
  them to `0x40` and `0x00`. A codec held in reset answers I2C with zeros, so an
  all-zero read is either the truth or nobody answering. The witness separates
  the two. A bad witness makes the read non-authoritative, and the case returns
  NOT-RUN.
- **A timeout is not proof of an unknown command.** For every case that tests
  whether a header exists, the suite asks `SYSTem:ERRor:NEXT?`. libscpi files
  `-113 undefined header` there. A silent reject and a lost byte look identical
  from the client, so the error queue is the only positive evidence.
- **SCPI runs over the kernel raw MIDI node.** `csd_diag.py --backend rawmidi`
  is the only path that has ever returned replies on this bench. The ALSA
  sequencer tools accept writes and answer nothing.

## 4. Case 01 — Mute silences, unmute restores

**Asserts.** The UAC2 playback mute removes the acoustic output, and unmute
brings it back to the original level.

**Measures.**
- Interfaces: ALSA playback through `ffmpeg` to `plughw:<dac>,0`; ALSA capture
  on the bench microphone card; `amixer` for the mute control.
- Stimulus: 1 kHz stereo tone at -6 dBFS, 3.0 s, host volume 192.
- Capture: `arecord -f S24_3LE -r 48000 -c 2` for 2 s. Level is the peak in the
  1 kHz bin (5 bins wide, Hann window). Floor is the median of 7–11 kHz.
- Mute surface: every `Playback Switch` control. The card has no control named
  "Mute". Each switch is written and read back, and the read-back is matched on
  the state line (`: values=on,on`), not on the capability line's element count.

**Pass criterion.**
- Drop from the unmuted level to the muted level is at least 30 dB.
- The level after unmute is within 3 dB of the original level.
- A drop below 30 dB reports `MUTE DID NOT SILENCE`. A restore outside 3 dB
  reports `UNMUTE DID NOT RESTORE`.

**Premises.**
- A microphone card is present. Without one the case is NOT-RUN; software
  cannot hear a mute.
- Host volume is settable and verified at 192.
- The tone clears the floor by at least 6 dB on the unmuted leg, both before
  and after the mute write. Otherwise NOT-RUN.
- At least one `Playback Switch` (UAC2 mute) control exists and takes the
  written value.

**Known limitations.**
- The result is acoustic. Room noise, microphone placement and the 3 dB
  restore tolerance all enter the number.
- The case does not prove the mute is in the DAC. It proves the host-visible
  mute control silences the output.
- A codec register write is never used as a fallback. Writing a register whose
  semantics were never confirmed is not a test.

## 5. Case 02 — `*IDN?` serial over MIDI

**Asserts.** The device identifies itself with four IEEE-488 fields, and the
serial field is a real, stable unit serial.

**Measures.** `*IDN?` over raw MIDI, twice. The reply is split on commas.

**Pass criterion.**
- Exactly 4 fields: vendor, model, serial, firmware.
- The serial is not empty, not `UNAVAILABLE`, and not `0`.
- The serial matches `^[0-9A-Fa-f]{6,16}$`.
- The second read returns the same string as the first.

**Premises.**
- The control path answers at all. If `*IDN?` is refused and the error queue
  has an entry, the case is TODO. If there is no reply at all, the case is
  FAIL.

**Known limitations.**
- The case checks format and stability. It does not check that the serial is
  unique across units, or that it matches a programmed value.
- Field 4 (firmware) is printed but not compared with any expected version.

## 6. Case 03 — DAC register read and write over MIDI

**Asserts.** A register write over MIDI reaches the codec, reads back the same
value, and a restore returns the register to its original value.

**Measures.**
- `REGister:READ?` and `REGister:WRITE` with base64 payloads over raw MIDI.
- Scratch registers: `0x11, 0x12, 0x13, 0x14` (the L/R trim registers).
- Pattern: `0x5A, 0xA5, 0x3C, 0xC3`.
- Witness: registers `0x03` and `0x07` are read in the same transaction and
  must return `0x40` and `0x00`.

**Pass criterion.**
- The read-back equals the pattern exactly.
- The restore write is followed by a read that equals the original values
  exactly, so the board is left as it was found.

**Premises.**
- The baseline read is authoritative: the witness pair must read `40 00`. If
  not, the case is NOT-RUN.
- The write is accepted. A write refused with an error-queue entry is TODO; a
  write with no reply is FAIL.

**Known limitations.**
- The scratch registers are the trim registers, not spare memory. A failed
  restore leaves the board at a non-default trim, and the case says so in the
  verdict.
- The case proves the I2C write path and the codec's register file. It does not
  prove that a given register bit does what the datasheet says.
- `REGister:WRITE` to codec control registers can put the board in a state that
  only a physical power cycle escapes. Register `0xC0` (SOFT_RESET) did exactly
  that during the latch investigation. Do not use it as a scratch target.

## 7. Case 04 — Persistent store read and write over MIDI

**Asserts.** A trim pair written over MIDI reaches the QSPI journal and is
adopted after a firmware reload.

**Measures.**
- `VOLume:TRIM:STATus?`, `VOLume:TRIM:LEFT`, `VOLume:TRIM:RIGHT`,
  `VOLume:TRIM?` over raw MIDI.
- Units are signed COUNTS of 0.125 dB, 0 (max) to -256 (-32 dB). They are not
  dB. The test pair is `-96, -24` counts, which is -12.0 dB and -3.0 dB.
- For a save command, the case probes `VOLume:TRIM:SAVE`,
  `PERSISTence:SAVE`, `DIAG:PERSISTence:SAVE` in that order, and asks the error
  queue for each.
- Persistence is proved by a reload, not by `VOLume:TRIM:LOAD`. The case runs
  `rig/load_xe.sh` on
  `bin/2AMi2o2xxxxxd/ClearSoundDac_2AMi2o2xxxxxd.xe`, then polls
  `VOLume:TRIM?` up to 20 times with 2 s spacing while the device
  re-enumerates.

**Pass criterion.**
- The pair read after the reload equals `[-96, -24]`.
- If it reads `[0, 0]`, the record was never adopted. That is FAIL with the text
  `survived the save but NOT the boot`.
- Any other value is FAIL: neither the saved pair nor the default.

**Premises.**
- The read side exists (`VOLume:TRIM:STATus?` answers).
- A save command exists. Today none does, so the case returns TODO with the
  live read-back printed. TODO is not a pass.
- `load_xe.sh` works and prints `PROVEN`. Without it the flash value and the
  mailbox value cannot be separated, and the case is NOT-RUN.
- The mailbox resets to 0 dB on reload. This is what makes the test valid: a
  value that appears after a reload could only have come from the record.

**Known limitations.**
- `VOLume:TRIM:LOAD` cannot distinguish flash from mailbox in this firmware,
  because every trim set journals immediately. Only a fresh boot separates the
  two. An earlier rig saved zeros and then convicted the journal of returning
  zeros.
- The reload is a firmware restart through `xrun`. It is not a power cycle. A
  raw power cycle has not been run.
- A flash that writes the data partition, a full-chip reprogram, or a different
  `--boot-partition-size` erases the record. A plain DFU update does not, and
  that leg was measured separately by `rig/trim_dfu_survive.py`.
- Source-level defect: the NOT-RUN return on the `load_xe.sh` failure path
  (line 441 of `csd_suite.py`) uses the undefined name `NOT_RUN`. The suite
  catches the resulting exception and reports NOT-RUN with `harness exception`.
  The status is correct; the detail text is lost.

## 8. Case 05 — DFU firmware update, twice in a row

**Asserts.** The upgrade path works from the field: flash the image over USB,
verify audio, flash again, verify audio.

**Measures.** Not implemented. The case returns TODO in both branches:
- Without `--allow-dfu`: gated, with the owner directive quoted.
- With `--allow-dfu`: `DFU double-flash not yet implemented in this suite`.

**Pass criterion (when implemented).** Flash, verify audio, flash again, verify
audio. The second pass is the requirement. A first pass can succeed while
leaving the new image's own DFU class broken.

**Premises.**
- Host DFU tooling must exist. Neither `xdfu` nor `dfu-util` is installed on
  this machine, and no DFU flash command is recorded in this repo. Any command
  line in a document or script that claims to do DFU is unverified.
- The first run must establish and record: how the board enters the DFU state
  and whether that survives a power cycle; the USB ID it presents in that state,
  confirmed with `lsusb`; and the exact flash command with the erase and
  read-back that prove it landed.
- Flashing over the debug probe is a different path. `xflash` is the bench and
  recovery route, and it does not test DFU.

**Known limitations.**
- This case has never run. It is the only TODO in the current status.
- A DFU update is proven only by `*IDN?` after the reboot. A successful flash
  command proves nothing about which image runs.

## 9. Case 06 — Bit-perfect at the maximum sample rate, DSP bypassed

**Asserts.** With every stage between host and DAC at the identity, the stream
the DAC transmits is bit-identical to the bytes the host sent, and dead silence
in produces all zeros out.

**Measures.** The case calls the gate through `run_gate()`:
1. Read `DEBUG:LOOPback?` before anything. If it does not answer, NOT-RUN: the
   gate's arm and clear commands cannot land, and the gate can only score
   zeros.
2. Arm the tap and clear it. Play a 2 s tone. Read `DEBUG:LOOPback?` again and
   parse the frame counter (second number). If the counter is not above zero,
   disarm and return NOT-RUN. A zero here is not evidence about the data path.
3. Run `rig/csd_bitperfect_gate.py --minutes N` (2.0, or 0.5 with `--quick`).
4. Disarm the tap on every path.
5. Decide from the `GATE:` line, never from the exit code.

Gate details:
- Card `hw:<n>,0`, S32_LE, 2 channels, 384000 Hz. The rate is `CSD_RATE`,
  default 384000.
- Playback ring 8192 frames (21 ms). Capture ring 32768 frames (85 ms).
- The whole schedule is generated into RAM before audio starts. The generator
  is off the streaming path, so numpy never competes with the writer.
- The stimulus is band-limited noise indexed by absolute frame number. A
  repeating buffer would make a gap of exactly one period invisible.

**TEST 1 — silence in, must be all zeros.**
- Send dead silence for 3 s with the tap armed.
- PASS requires zero non-zero words on both channels and at least one captured
  frame.
- A non-zero capture prints the most common words and classifies them: more
  than 8 distinct non-zero values on both channels is live ADC data on the host
  stream (FAIL); a constant value is an internal source such as a DSP bias or a
  stale hold.

**TEST 2 — band-limited noise in, must be bit-exact.**
- Stream for N minutes. Align the first captured pair by content against the
  played schedule, then track offset read by read.
- Score every read in full at its tracked offset with `bp_score.score_chunks`.
  Differing BITS are counted, not just differing frames. Slips are counted
  separately, because a slip is a property of the capture path, not of the
  audio data.
- The reported latency is the median out-to-in delay. It includes the playback
  ring, and the ring sizes are printed with it.

**Pass criterion (all of these, on the score that excludes the first 1 s after
the stream joins).**
- exact == checked, so every scored frame is bit-exact.
- checked > 384000, so at least one second of frames was scored.
- Non-zero tap coverage > 0.999.
- Capture read errors at most 16. A lost read is an unmeasured frame, not a
  wrong one.
- Differing bits == 0.
- Zero-word frames == 0.

**Premises.** All of section 3, plus:
- DSP preset 0. The gate aborts with `ABORT: DSP preset is not none` if not.
- Generator off. The gate aborts if the mode field is not 0.
- Host volume provably at maximum: every per-channel value line must read
  `[100%] [0.00dB] [on]`. The gate aborts otherwise.
- Mixer gains at 32768/32768. The gate aborts otherwise.
- The tap delivers frames. Without that the case is NOT-RUN.
- The sustained feed is within 2 % of 384000 Hz. An under-fed playback stream
  makes lib_xua substitute the underflow word (0 for PCM), which produces
  zeros that read exactly like a tap fault. The gate prints a warning; the
  result is not attributable to firmware.
- The gate produces a verdict line. If it does not, the case is NOT-RUN.

**Known limitations.**
- The tap is post-DSP, post-generator, post-mix and post-volume. It reports the
  word about to go on the I2S bus. Bit-exactness is only meaningful because
  every one of those stages is pinned to the identity.
- The first 1 s after the stream joins is excluded as a start ramp, and the
  gate prints both numbers so the two account for each other.
- At 600 MHz the copy-based tap cannot service every frame. See section 19.
- A skipped gate leg is VOID. The gate exits 0 when a leg did not run, so the
  suite treats an all-SKIP verdict as NOT-RUN.

## 10. Case 07 — DSP enabled, bit-perfect at the maximum rate

**Asserts.** With the DSP in the path, the stream is bit-transparent.

**Measures.**
- If `DIAG:DSP:PRESet?` does not answer, the image has no DSP and the case is
  TODO with the build flag named (`PCM_DSP_EN=1`).
- Read the current preset, set preset 2, run the same gate as case 06, restore
  the preset.

**Pass criterion.** A bit-transparent kernel (unity gain or pure delay) loaded
over MIDI, verified by the same TEST 1 / TEST 2 criteria as case 06.

**Premises.**
- The firmware has a DSP and the preset is selectable.
- A bit-transparent kernel is loadable. None is today.

**Known limitations.**
- The tap is post-DSP, so a processing preset is *expected* to differ from the
  input. Demanding bit-exactness through preset 2 would test the wrong thing.
  The case therefore returns TODO with the dependency named, and it does not
  borrow a pass from the bypass leg.
- If the gate does pass through preset 2, the case returns PASS with the text
  `suspicious: verify the kernel really ran`. A PASS here is a signal to
  investigate, not a clean result.
- Restoring the preset is best effort. A failed restore leaves preset 2
  selected.

## 11. Case 07.1 — DSP kernel read and set over MIDI

**Asserts.** The DSP preset can be read, changed, and restored over MIDI.

**Measures.** `DIAG:DSP:PRESet?`, then `DIAG:DSP:PRESet 2`, then
`DIAG:DSP:PRESet?`, then restore the original, then `DIAG:DSP:PRESet?` again.

**Pass criterion.**
- Both read-backs are non-empty.
- The value after setting 2 differs from the value before.
- The value after the restore equals the original.

**Premises.** The query answers. A refusal with an error-queue entry is TODO. A
timeout with an empty error queue is FAIL.

**Known limitations.** The case proves the control path, not that the selected
kernel does anything. Kernel behaviour is case 07.

## 12. Case 08 — Acoustic loopback, L/R, no swap

**Asserts.** A left-only input comes out of the left output, and a right-only
input comes out of the right output.

**Measures.**
- Interfaces: ALSA playback to the DAC, ALSA capture on the bench microphone
  card, `S24_3LE` at 48 kHz.
- Two legs. For each, a 1 kHz tone at -6 dBFS, 3 s, on one channel only, played
  at host volume 192. Capture 2 s. Peak per microphone channel at 1 kHz.
- Isolation is the driven channel's peak minus the other channel's peak.

**Pass criterion.**
- Isolation is at least 12 dB on both legs. Below 12 dB is FAIL with
  `isolation below 12 dB`.
- Neither leg shows a swap. A swap is `micL < micR` on the L-drive leg, or
  `micR < micL` on the R-drive leg. A swap is FAIL with `CHANNELS SWAPPED`
  regardless of the isolation numbers.

**Premises.**
- A stereo microphone card is present and its two channels are decoded
  correctly: bytes 0–2 are L, bytes 3–5 are R in the S24_3LE frame.
- Host volume is verified at 192.
- The device is not in the acoustic latch. In the latched state a left-only
  drive produces no output and a right-only drive appears on both outputs, with
  a channel correlation of 0.9998. That state survives a firmware reload, a USB
  reset, a rate change and a codec re-init, and needs a physical power cycle
  (see section 20).

**Known limitations.**
- The case has no probe-deaf guard. Unlike cases 01, 09 and 10, it does not
  require the tone to clear the floor by 6 dB, and it does not reject a tone
  that is below the floor. A dead microphone or a muted DAC therefore reads as
  a low isolation FAIL, not as NOT-RUN.
- The threshold is 12 dB, but the working bench measures about 42 dB. A result
  between 12 and 30 dB is a warning sign even though the case passes.
- The 12 dB floor is not a crosstalk specification. It is a swap detector.
- An uncalibrated microphone, and microphone placement, set the absolute
  numbers. Only the difference between channels is used.

## 13. Case 09 — Volume control

**Asserts.** Each host volume stage moves the acoustic level by the amount its
own control declares.

**Measures.**
- Read the declared scale from `amixer cget`: `min`, `max`, and `dBminmax-min`
  in dB. This card declares 0..192 over -48..0 dB, which is 0.25 dB per count.
- Every control named `Playback Volume` is a serial gain stage. On this card
  there are two, and they are not left and right: driving one alone moves both
  channels.
- For each stage: set every control to max for the baseline, then sample three
  points (max, midpoint, min) with the other stages explicitly written back to
  max.
- Capture: 1 kHz tone, 2 s, `S24_3LE`, peak at 1 kHz.

**Pass criterion.**
- Asked attenuation is `abs(dbmin) * (max - count) / (max - min)`.
- Measured attenuation is the drop from the baseline level.
- The best error over the two non-maximum points, across all stages, is at most
  4.0 dB.

**Premises.**
- A microphone card is present.
- The control declares a readable numeric range and dB range. Without one the
  case is NOT-RUN.
- The tone clears the floor by at least 6 dB at full volume. Otherwise NOT-RUN.

**Known limitations.**
- The baseline is taken at the control's maximum, so absolute accuracy at
  mid-scale is relative to that point.
- The other stages are restored with raw `amixer cset numid=<n> <max>` and are
  not read back, so a stage that refuses the write is not detected.
- The 4.0 dB tolerance is wide. It catches gain applied twice (measured once as
  a 48.5 dB drop for a 24 dB request) and a wrong dB scale. It does not certify
  step accuracy.
- Both attenuations must be compared as magnitudes. `dbmin` is negative and the
  drop is positive; an earlier version compared them directly and scored a
  correct -24 dB stage as a 47 dB error.

## 14. Case 10 — L/R volume trim independent

**Asserts.** The left trim moves only the left channel, and the right trim
moves only the right channel.

**Measures.**
- `VOLume:TRIM:LEFT` and `VOLume:TRIM:RIGHT` over raw MIDI. Units are counts of
  0.125 dB, so -6 dB is `-48` counts.
- Host volume 192, both trims set to 0 before the baseline. 1 kHz tone, 2 s
  capture on the bench microphone.
- After each single-channel trim, read registers `0x0F, 0x10` (channel volume)
  and `0x11..0x14` (master trim). These locate where the count landed.
- Each leg is released before the next one starts.

**Pass criterion.**
- Left trim: L falls 6 dB plus or minus 2.5 dB, and R moves at most 1.5 dB.
- Right trim: R falls 6 dB plus or minus 2.5 dB, and L moves at most 1.5 dB.

**Premises.**
- A microphone card is present.
- `VOLume:TRIM?` answers. A refusal with an error-queue entry is TODO; a
  timeout with an empty error queue is FAIL.
- Host volume is at maximum.
- The tone clears the floor by at least 6 dB.

**Known limitations.**
- The measurement is acoustic, so microphone channel separation sets the
  floor for "the other channel moved". On this bench that separation is about
  42 dB.
- Both trims landing in master trim was a real measured failure of this case,
  and the register dump exists to localise it. The dump is diagnostic text. It
  does not change the verdict.
- The trim registers are also the scratch registers of case 03. A case 03
  restore failure can leave a trim in place for this case.

## 15. Case 11 — Built-in microphone returns data while audio plays

**Asserts.** With power saving disabled, the DUT's built-in microphone
delivers non-constant data while audio plays.

**Measures.**
- Interfaces: `POWER:TIMEout` over raw MIDI, ALSA playback to the DAC, ALSA
  capture on the DAC card itself (`hw:<dac>,0`, `S32_LE`, 48 kHz, 2 channels,
  3 s).
- Set `POWER:TIMEout -1`, wait 1.5 s for the force-on state to reach the codec.
- Confirm the card offers an `S32_LE` capture stream with
  `arecord --dump-hw-params`. Without one the case is NOT-RUN (no capture
  stream on the DAC card).
- Play a 6 s tone, wait 1 s, then capture 3 s. Stop the tone.
- Per channel: count distinct sample values; compute RMS; compute spectral
  flatness, the strongest autocorrelation lag in 64..4000, and the dominant
  line ratio.

**Pass criterion.**
- At least 100 distinct values on each channel.
- RMS at least -140 dBFS on at least one channel.
- Flatness, autocorrelation and the dominant line are printed as information
  only. They are not gated. A bench microphone beside a switching supply has a
  dominant line, and rejecting that measures the room.

**Premises.**
- `POWER:TIMEout?` answers. Without it the case is NOT-RUN: power saving
  cannot be disabled, and the criterion does not apply.
- The capture stream opens on the DAC card.
- Audio is playing during the capture. This is the condition the owner
  specified.

**Known limitations.**
- A constant capture with **no** stream is accepted behaviour and is never a
  verdict about the microphone. The ADC has no control registers and streams
  whenever its clock is present. At a fresh load with no stream, one distinct
  value in 96,000 samples is expected, because the codec is not yet up.
- The distinct-value count cannot prove the data is acoustic. A firmware frame
  counter also produces many distinct values; that is why whiteness and
  periodicity are reported. They are not gated, so a stamp-like stream would
  still pass this criterion.
- The case restores the previous timeout in a `finally` block, and that restore
  is best effort.
- Do not gate this path on the idle power-down sequence. A register-less ADC
  cannot report a lost clock; it sends zeros.

## 16. Case 12 — Serial number in the USB descriptor

**Asserts.** The USB device descriptor carries a serial number, and that serial
agrees with `*IDN?`.

**Measures.**
- `lsusb -d 20b1: -v`, and the first `iSerial <n> <string>` match.
- `*IDN?` field 3 for the cross-check.

**Pass criterion.**
- The `iSerial` field exists.
- It is not empty, not `0`, and not `[00000000]`.
- If the MIDI serial is available, the descriptor serial equals it. The
  comparison is case-insensitive.

**Premises.**
- The device presents vendor ID `20b1`.
- `lsusb -v` can read the descriptor. If no `iSerial` is found the case is
  FAIL, which assumes the device is present and the descriptor is readable.

**Known limitations.**
- The case takes the first `iSerial` match on the first `20b1:` device. With
  two devices of that vendor on the bus, the match may belong to the other one.
- If the MIDI serial is unavailable, the cross-check is skipped and the case
  still passes with `MIDI serial unavailable for the cross-check`.

## 17. Case 13 — 24-bit / 384 kHz as advertised

**Asserts.** The descriptor advertises at least 24 bits and 384 kHz, and a
384 kHz 24-bit stream actually opens and plays.

**Measures.**
- `lsusb -d 20b1: -v`. Each `bSubslotSize` is paired with the
  `bBitResolution` that follows it. That pair is the container and the
  precision the FORMAT_TYPE_I descriptor declares.
- Container mapping: `(4,24)` and `(4,32)` are `S32_LE`; `(3,24)` is
  `S24_3LE`; `(2,16)` is `S16_LE`.
- Render a 1 s 384 kHz stereo sine in the advertised container with `ffmpeg`.
- Read the playback rate range with `aplay --dump-hw-params`.
- Play the file with `aplay`.

**Pass criterion.**
- The descriptor advertises at least one mode with `bBitResolution >= 24`.
- The playback rate range maximum is at least 384000 Hz.
- The 384 kHz stream plays with return code 0.

**Premises.**
- The descriptor contains FORMAT_TYPE_I subslot/resolution pairs. Without them
  the case is NOT-RUN.
- The advertised container has a known ALSA mapping. Without one the case is
  NOT-RUN.
- The test file renders.

**Known limitations.**
- This card advertises 24 valid bits in a 4-byte container, which is `S32_LE`,
  and does **not** offer a 3-byte `S24_3LE` container. Demanding `S24_3LE` is a
  bug in the test. An earlier version of this case did exactly that and
  reported "descriptor lies" about a descriptor that told the truth.
- The case checks the first 24-bit-or-better mode only.
- The case does not check the bit depth on the wire, and it does not check that
  the low 8 bits of the 32-bit container are zero. Bit-exactness is case 06.

## 18. Case 14 — Power saving enter/exit and timeout control

**Asserts.** The device enters and leaves power saving, and the timeout control
holds the value it is given.

**Measures.**
- Runs `rig/idle_power_cycle.py` with a 900 s timeout. That script proves the
  DAC really went down and that volume returns, by reading the codec's own
  registers (`0x03` analog volume, `0x0F/0x10` per-channel digital volume,
  `0x07` filter shape / system mute) rather than a log line. Volume is moved
  off 0 dB first, because at the chip default a broken restore is
  indistinguishable from a working one.
- Then `POWER:TIMEout?`, `POWER:TIMEout 45`,
  `POWER:TIMEout?`, `POWER:TIMEout -1`, `POWER:TIMEout?`, `POWER:TIMEout 0`,
  wait 2 s, `POWER:TIMEout 300`.

**Pass criterion.**
- The read-back after setting 45 is `45`.
- The read-back after setting -1 is `-1` (or `4294967295`).
- Anything else is FAIL: `timeout control does not stick`.

**Premises.**
- `POWER:TIMEout?` answers with a non-empty value. If it does not, the case is
  TODO and names the missing firmware: `POWER:TIMEout?` and
  `POWER:TIMEout <sec>` with the owner semantics (0 = sleep now and stay
  asleep, negative = never sleep, positive = seconds, default 300), plus
  persistence of any non-zero value in the QSPI journal and application at
  boot.
- The idle-power script's expectations must match the current sequence. A
  stale instrument premise is not a verdict. Registers `0x0F/0x10` are written
  by `vol_apply_digital`, and `pw_volume_to_minimum()` reaches them through
  `vol_apply_host_request(VOL_INTERNAL_MIN_DB8)`, so re-run the rig before
  restoring any step.

**Known limitations.**
- Persistence across a power cycle is a separate leg and is not measured here.
- The idle-power script also measures the enter/exit leg. Its exit code is
  recorded in the detail text as `enter/exit rc=<n>`, but the case verdict is
  decided by the timeout control only.
- On image `.85` the header does not exist: `POWER:TIMEout?` answers
  `-113,'Undefined header'`.

## 19. Case 15 — Upsampling option: XMOS 8x with the codec's FIR bypassed

**Asserts.** The option exists, defaults to off, survives a power cycle when set,
and never engages unless the codec is genuinely being fed a 384 kHz or 352.8 kHz
frame clock.

**Build note.** The option is compiled in with `-DCSD_UPSAMPLE_EN=1`. Without it
the rows do not exist. Filter math is qualified offline by
`tools/dsp/xc_sim/run_sim_qualification.sh`, which must print `IDENTICAL` on both
the host and the XS3 simulator.

**Why one control.** Enabling is a single decision covering the interpolator and
the ES9219Q's own oversampling FIR (reg 7 bit 3). 8x data with the chip FIR still
engaged interpolates twice; the FIR bypassed without 8x data starves the
modulators. Both produce silence or noise, so the pair cannot be set apart.

**Steps.**
1. `DIAG:UPSAMPLing:STATe?` at boot, before any command.
2. `DIAG:UPSAMPLing:STATe 1` at 48 kHz, then `DIAG:UPSAMPLing:STATe?`.
3. Set the host stream to 384 kHz, repeat step 2.
4. Power-cycle, `DIAG:UPSAMPLing:STATe?`.

**Reply shape.** `xmos_en,applied,frame_hz,codec_refusals,ups_refusals`. The
reply is always the resulting state, never the request.

**Pass criterion.**
- Step 1: `0,0,<frame>,0,0` -- off, with the chip's built-in filter in circuit.
- Step 2 (enable, before the stream restart): `applied=0` and `codec_refusals`
  increments by one. This is the interlock working, not a failure: at a 48 kHz
  frame clock the modulators would be starved by a factor of 8. After the stream
  restart the same request must read `applied=1, frame_hz=384000`.
- Step 4: the value set in the last successful `STATe 1` returns.
- With the option off, the case-06 bit-perfect result must be unchanged, because
  off means the shipped signal path is untouched.

**Measured on the bench (revBB, image 781bca37, host stream held at 48 kHz).**
Codec state read back over I2C, not taken from firmware:

| option | `STATe?` | reg `0x07` | reg `0x0A` | reg `0x40` |
|---|---|---|---|---|
| off | `0,0,48000,0,0` | `0x00` (FIR in circuit) | `0x02` | `0x88` |
| on  | `1,1,384000,1,0` | `0x08` (OSF bypassed) | `0x12` (128FS set) | `0x89` (lock bit up) |
| off | `0,0,48000,1,0` | `0x00` | `0x02` | `0x88` |

`applied=1` at `frame_hz=384000` with the host still at 48 kHz is the result:
the frame clock the codec receives is set internally, the chip's own FIR comes
out, and `128FS_MODE` follows from the `mClk == 128*fs` test.

**Register reads must use the proven wire form** -- quoted base64 of the raw
address byte (`REGister:READ? "AA=="` for reg 7). A plain `0x07` is rejected with
`-101`, and a rejected reply base64-decodes to `b''`, which reads back as
register value 0: a gate that cannot fail. See section 21.

**Timing note.** Enabling lands at the NEXT stream start, because the force lives
in `csd_AudioHwConfig`. The first attempt at a 48 kHz frame clock is counted as a
refusal before the restart grants it, which is why `codec_refusals` reads 1
after a successful enable.

**Not yet run.** The power-cycle leg (step 4), and case 06 re-run with the option
off to reconfirm the shipped path is untouched.

## 20. Current status (2026-11-06)

Measured on the bench. These are results, not intentions.

**At 800 MHz, on the flashed image: 14 PASS / 0 FAIL / 1 TODO.**

- The only TODO is case 05 (DFU). It has never run: no `xdfu` and no
  `dfu-util` are installed, and no DFU command is recorded.
- Case 06 measured `46,075,904/46,075,904 = 100.0000%` bit-exact. That number
  is from the gate after the scoring fix described in section 21.
- Cases 11 and 14 passed, so that image answers `POWER:TIMEout?` and
  `DEBUG:LOOPback?`. Image `.85` rejects both with `-113`.

**At 600 MHz: case 08 passes, case 06 fails, and the failure is the
instrument.**

- Case 08 PASS at 42.0 dB and 42.4 dB isolation.
- The audio engine shows 0 underruns and 0 overruns, with and without DSP
  preset 2.
- Case 06 FAIL. The copy-based loopback tap starves at that clock: coverage
  0.9331, 771,072 zero words, 2204 slips.
- The cost is instrumentation, not audio. The fix is the pin-32 hardware
  loopback hook in `I2S_LOOPBACK_HOOK.md`: wire `DATA DAC0` (pin 14) to the
  spare `DATA ADC1` input (pin 32, `XS1_PORT_1F`), read the word back from the
  pin instead of copying it through the RAM array, and keep the MIDI arming
  command unchanged. The proof required after the rework is: the
  `DEBUG:LOOPback?` counter still advances; coverage 1.0000 with zero-word 0
  and slips 0 at 600 MHz; case 06 PASS with 0 differing bits at 600 MHz; then
  re-measure 800 MHz for no regression.

## 21. Known traps that have produced false results

Each item below caused a wrong verdict on this bench. Check it before you
believe a FAIL.

- **Gate scoring bug.** The original scorer counted only the frames that
  matched before the first mismatch in each read, then re-synced by content and
  never scored the rest of that read. With one framing slip per read it
  reported about 0.0033 % for a stream whose every sample was correct. It could
  not tell a firmware bit error from a capture bookkeeping slip, and both
  printed the same near-zero number. `bp_score.score_chunks` now predicts the
  offset, verifies the prediction against the read head, scores the whole read,
  and counts differing bits and slips separately.
- **Reading a stale image after a failed flash.** A wrapper that piped
  `xflash` output masked `xflash`'s exit status, so a flash that failed looked
  successful and every later measurement described the previous image. Capture
  the exit code before piping. Then prove which image runs: read `*IDN?` and
  compare the version stamp inside the `.xe`. A filename or a successful flash
  command proves nothing. Archive the boot flash before you overwrite it.
- **`amixer` numid numbers differ between firmware shapes.** On `.85`, card 3
  `numid=5` is the boolean `Mix 2 Playback Switch`, not a volume. A blind
  `amixer cset numid=...` pokes the wrong control and silently produced a false
  "no 1 kHz tone at all" report. Enumerate `amixer -c <card> controls` first
  and address controls by name. For a boolean switch, match `: values=on,on`;
  the capability line's `values=2` is the element count.
- **S24_3LE bench microphone decode.** Bytes 0–2 are L and bytes 3–5 are R. A
  `np.stack([v, v])` duplicate-channel decode was written once and hid a real
  result.
- **The digital mirror probe is not evidence.** `rig/mixer_mirror_probe.py`
  fails its own self-check even on a fresh reload
  (`FAIL SELF-CHECK: mirror carries a real tone ... peak -55.0 dBFS`). A dead
  mirror is an instrument fault. It must be fixed before it can exonerate or
  convict anything.
- **`POWER:TIMEout` and `DEBUG:LOOPback` do not exist on `.85`.** That image
  answers `-113,'Undefined header'` to `POWER:TIMEout?`, `DEBUG:LOOPback?` and
  `DEBUG:I2S?`. A timeout there is an absent command, not a broken device.
- **A silent SCPI reject looks like a transport fault.** Ask
  `SYSTem:ERRor:NEXT?` before you conclude "no reply". Drain the FIFO first: a
  stale `-113` from an earlier probe once blamed the wrong command.
- **The tap carries samples, or the zeros mean nothing.** `DEBUG:LOOPback?`
  reporting enabled with `tx_frames=0` makes a capture that is legitimately all
  zeros, and the gate scores 0.0000 non-zero on both channels, which is
  indistinguishable from a perfect stream. The suite now refuses to run the
  gate unless the tap delivers frames.
- **A skipped leg is not a pass.** The gate exits 0 when a leg did not run. An
  earlier version defaulted both skip flags true and printed `TEST2=PASS` for a
  leg that compared no frames.
- **A tone clears the floor, or nothing is measured.** An early floor used bins
  20–200 (10–100 Hz mains hum and room rumble) and read higher than the tone,
  so a working mute tested "deaf".
- **The acoustic latch is invisible to the host.** In that state a left-only
  input produces no output and a right-only input appears on both outputs, with
  channel correlation 0.9998 and isolation collapsing from 42 dB to +0.8 dB.
  Every ALSA control reads nominal and the codec routing registers are
  unchanged: `0x01=0x8C`, `0x02=0x34`, `0x0D=0x00`, `0x0F/0x10=0x00`. Only an
  acoustic measurement detects it. A firmware reload is not a codec power-on:
  the firmware never asserts the codec reset GPIO, and it has no write site for
  `0xC0` SOFT_RESET. A board left in the post-`0xC0` state needs a physical
  power cycle.
- **The mixer and its boot tables are live on `.85`.** That build is
  `PCM_DSP_EN=1 CSD_DIAG_MIDI=1 CSD_DSP_PRESET_DEFAULT=2 CSD_GEN_EN=1
  CSD_GEN_INSTRUMENT MIXER=1 MAX_MIX_COUNT=2`. Reading MIXER defaults out of
  `xua_defs.h` gives `MIXER=0` and does not describe the release shape.
- **A named model on the card label depends on the build.** The release image
  is `DACUAC20`; the debug image is `XMOSUAC20`. Matching one spelling made
  every gate of the other shape report "no DAC found".

## 22. Open dependencies

These are the reasons the remaining non-PASS results exist. They are work
items, not verdicts.

- **Case 05** — install or verify a DFU host tool, record how the board enters
  DFU and the USB ID it presents, implement the double-flash sequence.
- **Case 04** — firmware must expose a save command (`PERSISTence:SAVE` is the
  suggested name). The case then proves survival across a reload.
- **Case 07** — a unity-gain or all-pass DSP kernel must be loadable over MIDI
  before a processing preset can be certified bit-transparent.
- **Case 06 at 600 MHz** — the pin-32 hardware loopback hook. Until then, run
  the bit-perfect cases at 800 MHz and say so.
