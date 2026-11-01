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
