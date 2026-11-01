# AGENTS.md — ClearSoundDac firmware

App repo for the CSD USB DAC (XU316 + ES9219Q). Build/ops procedure lives in
the `clearsound-dac-xmos` skill; action log in `../LOG.md` (ub-mode).
Release: `release/` + tag `v2026.11.01.31`.

## TODO — features (added 2026-11-01, user directive)

1. **Remove automute handling from ES9219Q code.** Do nothing to automute:
   leave the register at the DAC's default condition, whatever that is. No
   writes, no reads used for decisions, no "disable automute" boot writes.
2. **Add DC as a function-generator output type.** New waveform mode next to
   sine/square/noise/sweep/tone in the `GENerator:` SCPI family.
3. **L/R channel trim with persistence.** A small volume difference between
   L and R must be supported and survive reboot:
   - persistent data store for the difference (survives power cycle),
   - two adjustments: `Vol_trim_L`, `Vol_trim_R`,
   - each: max 0 dB, step 0.125 dB, min −32 dB,
   - default both to 0 dB on reset/power-up if no valid persisted record
     found (validity = magic + checksum, not just non-erased flash).
4. **Trim application point.** Whenever a volume change happens, each
   `Vol_trim_*` is added to its respective L/R channel — the trim rides on
   every volume update, host SET_CUR and button paths alike.
5. **Move debug-only SCPI under `DEBUG:` with a compile gate.** Many commands
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
6. **Release build flag.** Release builds enable `SPECIALTYCIRCUITS_RELEASE`.
   This same flag is the gate that disables all debug functions (items 5's
   SCPI set, and any future bench-only code): release = `-DSPECIALTYCIRCUITS_
   RELEASE`, debug/bench builds omit it. It already exists in
   `src/core/xua_defs.h` (vendor/product strings) — extend its meaning to be
   the single release switch.

## Merge notes (2026-11-01, mvp → main)

- Merge of `origin/mvp` (16de3fa): pin configuration taken from mvp
  (`src/ClearSoundDac.xn`); main's copies preserved under `archive_revBB/`
  with `_revBB` suffix (board.xc, es9219q.h, ClearSoundDac.xn). `archive_revBB/`
  sits OUTSIDE `src/` because XMOS cmake globs every `*.xc` under `src/` and
  would double-compile the archived copies.
- es9219q.h resolution keeps main's datasheet-correct MUTE/UNMUTE pair
  (reg 7 bit0: 1=mute) — mvp carried the pre-fix inverted copy; callers in
  board.xc use these macros by name. mvp's real edit (FILTER_SHAPE 1→0) is
  taken.
