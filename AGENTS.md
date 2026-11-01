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
