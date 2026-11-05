# TODO — ClearSoundDac firmware

Owner-directive feature list. Items land via ub-mode (see `../LOG.md`);
one commit per item, acoustic gate (rig mix + mute) after each.

## Features (added 2026-11-01)

Status: ALL SIX DONE (2026-11-01), released as `v2026.11.01.85-346fe14`.
Item 3's persistence is implemented AND verified on hardware (both builds) --
it is no longer compiled out; see item 3 for the evidence and for the two legs
still open (raw power cycle, trim survival across a DFU update).

1. **DONE (2026-11-01) — Remove automute handling from ES9219Q code.** Do nothing to automute:
   leave the register at the DAC's default condition, whatever that is. No
   writes, no reads used for decisions, no "disable automute" boot writes.
2. **DONE (2026-11-01) — Add DC as a function-generator output type.** New waveform mode next to
   sine/square/noise/sweep/tone in the `GENerator:` SCPI family.
3. **DONE (2026-11-01) — L/R channel trim with persistence.** A small volume difference between
   L and R must be supported and survive reboot:
   - persistent data store for the difference (survives power cycle),
   - two adjustments: `Vol_trim_L`, `Vol_trim_R`,
   - each: max 0 dB, step 0.125 dB, min −32 dB,
   - default both to 0 dB on reset/power-up if no valid persisted record
     found (validity = magic + checksum, not just non-erased flash).

   Done and verified 2026-11-01: the two adjustments, the 0 dB max / 0.125 dB
   step / -32 dB floor contract (rejected AND reported via the error queue),
   and the 0 dB / 0 dB power-up default. Surface is production SCPI
   (`VOLume:TRIM:LEFT|RIGHT|?`), gated into the release command list.
   `csd_trim_store.c` implements the journal (append-only 32 B slots in one
   4 KiB sector, magic + version + length + checksum, newest-valid-wins).

   PERSISTENCE VERIFIED 2026-11-01 by `rig/trim_persist_run.sh` (two-phase:
   arm, tear xrun down, load the firmware again, verify). Armed pair -64/-128
   counts (-8/-16 dB) read back as [-64,-128] after the reload with
   partition=1048576, load=1, journal seq preserved (11==11), err=0, boots=1,
   and saves=0 proving the load path is read-only. The microphone confirmed
   the reloaded values drive the DAC (-8.97 / -16.96 dB measured; both legs
   sit ~1 dB low together, the known cold-start bias on a life's first
   measurement -- absolute trim accuracy is proven separately by
   rig/trim_acoustic_ab.py at -7.99/-8.00 and -0.126 dB per 0.125 dB step).
   A power cycle has NOT been run: it exercises the same boot-load code path
   over the same non-volatile sector, but the evidence above comes from a
   firmware restart via xrun teardown/reload and from firmware status words,
   not from a raw dump of the current journal contents. Owner power-cycles
   the board when convenient to close that last leg.

   PERSISTENCE DATA GETS WIPED — owner-acknowledged 2026-11-01 ("ok to flash,
   add note to todo about persistence data get wiped"). The trim record lives
   in the flash DATA partition, which is NOT part of what a firmware update
   preserves. It is erased by any of:
     - `xflash --data ...` (an explicit data-partition write),
     - a full-chip reprogram (`xflash --write-all`, or any recipe that
       re-lays-out the boot partition),
     - a different `--boot-partition-size` than the one that placed the data
       partition, which moves or removes it.
   A plain DFU firmware update (`xflash --factory-version ... --upgrade 1` +
   the DFU path) writes boot-partition images only and MUST leave the trim
   intact — that leg was a rig requirement, not an assumption. **MEASURED AND
   PASSING 2026-11-01** by `rig/trim_dfu_survive.py` (+ scratch wrapper): arm
   LEFT=−64 / RIGHT=−128 (−8 dB / −16 dB) → device reboot → the release DFU bin
   (`e944e3cc…`, boot partitions only, **no** `--data`) → exit DFU → fresh boot.
   On that fresh boot the device replies `VOLume:TRIM?` = `-64,-128` (unchanged)
   and `VOLume:TRIM:STATus?` = `1048576,1,0,1,0,1,0` — partition 1 MiB still
   present, `load=1` = a VALID record was found and adopted
   (`csd_trim_store.c:361-369`: 1=found, 2=nothing valid, 3=connect failed),
   `seq=1` identical to the armed journal (read from flash, not RAM) while the
   RAM `save_n` correctly reset to 0 for the new life, `err=0`. A zeroing leg
   (`rig/trim_dfu_survive.py zero`) then took the board back to `0,0` and
   advanced `seq` 1→3, proving the journal is still writable after the DFU and
   that the −64/−128 was not a stale RAM echo. Manufacturing consequence stands:
   set trim LAST, and re-check `VOLume:TRIM?` after any flash that touches
   `--data`, `--write-all`, or the boot-partition size — those three DO erase it.
   Consequence for manufacturing: set trim LAST, after the final firmware
   flash; re-verify trim with `VOLume:TRIM?` after any re-flash and re-run the
   persistence leg after any firmware update.

   API correction (2026-11-01, verified against XTC 15.3.0 headers): the first
   draft called `fl_read`, `fl_sectorErase` and `fl_pageWrite`. None of those
   exist in 15.3.0's quadflash.h — it would not have linked. The real set is
   the data-partition API: `fl_getDataPartitionSize`, `fl_readData`,
   `fl_writeData` (+ `fl_getWriteScratchSize`), `fl_eraseDataSector`,
   `fl_getNumDataSectors`, `fl_getDataSectorSize`. Because the API addresses
   the data partition by OFFSET, not by raw flash address, the boot-ROM XEF-scan
   brick risk this item was gated on does not apply — the tool owns placement.
4. **DONE (2026-11-01) — Trim application point.** Whenever a volume change happens, each
   `Vol_trim_*` is added to its respective L/R channel — the trim rides on
   every volume update, host SET_CUR and button paths alike.

   Applied as a standing Q2.29 multiplier folded into the mixer's
   output-volume multiplier (`csd_mix_apply_trim`, lib_xua 0c69d009), at both
   output-volume sites. Standing rather than re-applied is what makes "on
   every volume change" true by construction: there is no path that bypasses
   it. Verified: LEFT -8 dB measured -7.99/-8.00 dB with the opposite channel
   at +0.01/-0.01 (interleaved A/B/A/B), and one 0.125 dB step resolved at
   -0.117 dB — a step the ES9219Q cannot produce (0.5 dB/LSB), which is also
   the proof the multiplier is the right site. Not in the codec: with MIXER=1
   a host SET_CUR never reaches the app's codec volume path.
5. **DONE (2026-11-01) — Move debug-only SCPI under `DEBUG:` with a compile gate.** Many commands
   were added purely for bench debugging. Group them under a `DEBUG:` prefix
   and put them behind a selective compile flag so release packages do not
   contain them at all — table entries *and* their callbacks, not just the
   table rows (unreferenced statics still cost flash and keep mailbox hooks
   alive). The release build's command table must also shrink, so a stray
   `DEBUG:` command answers -113 "Undefined header".
   - Debug-only candidates (bench instrumentation added during bring-up):
     all `MIXer:*` proof-of-life counters (FRAMES/BUSY/TAIL/HWCfg/DACPeak),
     `DIAG:I2S?`/`DIAG:I2S:CLEAR`, `GENerator:VOLume?`, `GENerator:DOPpRObe?`,
     `GENerator:STATe:REJect?`, `GENerator:COUNts?`.
   - Keep in release (owner directive 2026-10-30 scope + rig dependency):
     `*IDN?`, error queue, `DIAG:DSP:PRESet`, `DIAG:IDENTity:*`,
     `REGister:READ?`/`WRITE`, `MIXer:GAIN:*` (per-source volume), and the
     `GENerator:` waveform setters (manufacturing rig drives them; revisit
     once factory tests move to a debug build).
6. **DONE (2026-11-01) — Release build flag.** Release builds enable `SPECIALTYCIRCUITS_RELEASE`.
   This same flag is the gate that disables all debug functions (item 5's
   SCPI set, and any future bench-only code): release = `-DSPECIALTYCIRCUITS_RELEASE`,
   debug/bench builds omit it. It already exists in `src/core/xua_defs.h`
   (vendor/product strings) — extend its meaning to be the single release
   switch.
   - Known side effect (handled 2026-11-01): the flag renames the USB product
     string to "ClearSound DAC (UAC2.0)"; the rigs and `csd_diag.py` match
     the card by `xmos` OR `clearsound`. SCPI `*IDN?` is unaffected (always
     answers `ClearSound,CSD-22,...`).

## Idle power (added 2026-11-04)

7. **TODO (owner directive 2026-11-04) — Idle-power shutdown / ordered restart.**
 The XMOS core monitors the audio stream. If NO audio stream arrives for
 more than 5 minutes, shut down in this exact order:
   1. amplifier,
   2. DAC,
   3. DSP and mixer.
 Restart on stream return, in this exact order:
   1. DSP and mixer start first (only if they are enabled),
   2. DAC next,
   3. amplifier last (deassert SUSPEND_N).
 Hard requirements from the owner:
 - Restarting the DAC means a FULL re-initialization (the boot init
   sequence, not just an un-mute), and volume must be restored to the value
   held before shutdown.
 - Before shutting the amplifier down, reduce volume to minimum first
   (pop suppression on the way into shutdown).
 Notes for whoever picks this up:
 - "No audio stream" must be defined precisely. A paused stream with the
   clock still running is NOT silence — the codec DPLL locks on clock alone
   with no samples on the bus (the instrument rule that burned a week).
   Candidate witnesses: endpoint activity, LRCLK-delivered samples, mixer
   frame counters (all readable on the audiohub / server tasks).
 - The 5-minute timer should ride an existing task's select.
   `AudioHwRemoteTile0` already owns mute, volume, LED state and I2C and is
   the natural owner. Do NOT add a thread to tile[0] without re-reading the
   census (`xobjdump --resources`; it is 5/8 threads, 27/32 chanends after
   the 2026-11-04 chanend diet).
 - Amplifier shutdown = drive SUSPEND_N; DAC shutdown = codec power-down
   register(s). The wake path must replay the ES9219Q bring-up including
   the PLL/chip-ID policy, not just release mute.
 - Volume snapshot/restore must go through the single-writer appliers
   (`vol_apply_host_*`). The host-facing USB volume cache in ep0
   (`volsOut`) must stay authoritative so a host `GET_CUR` during shutdown
   reads truth.
- Gate: acoustic (rig mix + mute) across a full shutdown/wake cycle, plus
   verification that wake restores trim, mute state, LED tier and DSP
   preset exactly as before.

## Acceptance suite gaps (added 2026-11-05, owner's 14-case list)

Suite: `csd-js-tester/rig/csd_suite.py`. Each case reports PASS / FAIL /
NOT-RUN / TODO. NOT-RUN indicts the fixture, never the firmware; TODO names
the missing dependency. Items below are the dependencies.

1. **POWER:TIMEout control + persistence (case 14).** No such SCPI header
   exists (`POWER:` appears 0 times in the image). Owner semantics: `0` =
   sleep now and stay asleep, negative = never sleep, positive = seconds,
   default 300 s; any non-zero value is stored in persistence and applied at
   power-up. Needs: `POWER:TIMEout?` / `POWER:TIMEout <sec>`, a runtime
   timeout in `csd_power.c`, and a field in the QSPI journal record
   (`csd_trim_store.c`, bump `CSD_TRIM_REC_VERSION` and invalidate v1).
2. **No persistence save command (case 4).** The journal read side is live
   (`VOLume:TRIM:STATus?` = 1048576,1,1,197,0,2,2; writing -96,-24 counts
   reads back -96,-24) but there is no header that commits a written pair to
   flash. Add `PERSISTence:SAVE`. Case 04 then stops being a TODO.
   Do not detect command existence by "did it time out" -- ask
   `SYSTem:ERRor:NEXT?`, and drain the FIFO before trusting an entry: a stale
   `-113` from an earlier probe once blamed the wrong command entirely.
3. **L/R trim not independent (case 10, REAL FAIL).** Setting LEFT to -48
   counts (-6 dB) moved BOTH channels ~5.4 dB; RIGHT-only moved both ~6.0 dB.
   The case now dumps regs 0x0F/0x10 (channel volume) and 0x11..0x14 (master
   trim) after each leg -- read that to localise it. Suspect both per-channel
   trims land in MASTER TRIM. Units are counts of 0.125 dB, signed, 0..-256;
   asking in dB is a harness bug, not a firmware one.
4. **Volume applied twice (case 09, REAL FAIL).** Control declares 0..192 over
   -48..0 dB (0.25 dB/count). Measured full(192) -21.0 dBFS -> 96 gave -69.5
   dBFS: a 48.5 dB drop for a 24 dB ask. Case now samples three points and
   prints the codec CH1/CH2 volume regs at each. Suspect the two channel
   controls summing into one register, or the stage applying twice.
5. **Bit-perfect unmeasurable (cases 6/7).** `DEBUG:LOOPback?` reports the tap
   enabled with `tx_frames=0`, and SCPI stops answering entirely while a
   stream is active (the gate's arm/clear commands return nothing). Consequence:
   the gate captures all zeros and scores it as a result. The suite now refuses
   to run the gate unless the tap delivers frames. Root cause to chase: diag
   task starvation on tile[0] under load (census: 5/8 threads, 27/32 chanends).
   `DEBUG:MIXer:FRAMES?`/`HWCfg?` are release-gated out, so a debug-shape image
   may be required to instrument this.
6. **Built-in mic (case 11).** Capture opens (S32_LE, 48k) but is a structured
   stream, not acoustic: spectral flatness 0.33/0.36, autocorrelation 0.93/0.96,
   dominant line +35.6 dB. That is the diagnostic stamp path, matching the
   standing finding. Confirm against the schematic whether an ADC/mic is fitted
   at all; if not, case 11 must be gated on hardware presence instead of
   failing forever. A distinct-value count cannot tell these apart -- a frame
   counter also has many distinct values.
7. **DFU twice (case 5).** Gated behind `--allow-dfu`: flashing is not
   authorised on this bench (owner directive 2026-11-05, RAM-only). Implement
   flash -> verify audio -> flash again -> verify audio; the second pass is the
   one that proves the new image's own DFU class still enumerates.
8. **idle_power_cycle.py expectations vs the simplified sequences (case 14).**
   Rig reports "reg15/16 unchanged at T1 -- the down sequence did not run".
   Check whether `sequence_down()` still drives channel volume to the floor;
   the pre-simplification state machine did. Either restore the step or fix the
   rig's premise -- but decide deliberately, do not let a stale instrument
   define the verdict.
   STATUS 2026-11-05: partially resolved by the per-channel trim work. Regs
   15/16 are now written by `vol_apply_digital` (proven: `0C 00` / `00 0C` for a
   6 dB LEFT/RIGHT trim), and `pw_volume_to_minimum()` reaches those registers
   through `vol_apply_host_request(VOL_INTERNAL_MIN_DB8)`, so the down leg has a
   route to the floor. Re-run the rig before restoring any step: the premise may
   simply have been stale.

9. **Bit-perfect tap has no audio-path call sites -- case 06 cannot pass.**
   `csd_mb_loop_put()`, `csd_mb_loop_put_site()` and `csd_mb_loop_take()` are
   defined in `csd_gen_mailbox.c` and read by the SCPI side, but are called by
   NOTHING in the tree (verified by grep across the app and lib_xua). The
   producer/consumer hooks belong in the audio hub's TX/RX loop -- the header
   still says so ("writer: audiohub TX", "reader: audiohub RX") -- and the
   detached lib_xua pin (`0c69d009`) does not contain them. The orphan comment at
   `csd_gen_mailbox.c:621` ("Undefined reference to csd_mb_loop_put") is the
   fossil of when they did.
   CONSEQUENCE: `DEBUG:LOOPback?` answering `1,0` is not a rate-specific fault at
   384 kHz. The tap has never run at any rate; frames stay 0 because nothing
   produces. Any earlier bit-perfect pass was measured against a patched
   lib_xua, not this pin.
   FIX: re-add the two hooks in lib_xua's audio loop, guarded by
   `CSD_LOOPBACK_TAP`, keeping the header's cost contract -- one volatile load
   plus a branch per channel per frame when disarmed, and tile-local state
   (tile[0] would silently read 0 and look healthy: the trim bug's shape).
   Needs a lib_xua branch decision from the owner before editing the pin.

10. **Case 04 persistence -- fixture was wrong, not the journal.**
    The rig set the pair, zeroed it, saved, then issued LOAD. Every trim set
    journals immediately, so the zeros reached flash before LOAD ran and LOAD
    correctly handed zeros back. `VOLume:TRIM:LOAD` cannot separate flash from
    mailbox in this design; only a fresh boot can (the mailbox resets to 0 dB, so
    a value that appears after a reload could only have come from the record).
    The rig now reloads the image via `rig/load_xe.sh` -- promoted out of scratch,
    which is pruned after 24 h -- and reads back after re-enumeration. Boot adopt
    is real: `csd_diag_midi.xc` calls `csd_mb_put_trim()` after the flash load.
