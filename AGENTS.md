# AGENTS.md — ClearSoundDac firmware

App repo for the CSD USB DAC (XU316 + ES9219Q). Build/ops procedure lives in
the `clearsound-dac-xmos` skill; action log in `../LOG.md` (ub-mode).
Release: `release/` + tag. Current: `v2026.11.01.85` (app 302bb22, DFU bin
md5 e944e3cc50bc631878b9959855fbfc53). Previous: `v2026.11.01.31`.

## Bit-perfect tap: ships in the release, armed only by MIDI

The loopback tap MUST be compiled into release images (owner directive
2026-11-05), so field verification is possible on shipping firmware. It is inert
by default -- `csd_mb_loop_en` is a file-static and therefore starts at zero --
and the only way to arm it is the MIDI SCPI row `DEBUG:LOOPback 1`, which sits
outside the release gate on purpose. So `-DCSD_LOOPBACK_TAP` belongs in the
release flag set next to `-DSPECIALTYCIRCUITS_RELEASE`.

Two invariants, because an armed tap rewrites the stream: `take()` returns the
zero-initialised `csd_mb_loop_tx[ch]` while armed, so an image left armed
delivers literal zeros to the host; and a release must never arm it at boot.
Anything that arms the tap disarms it on every exit path, failures included.

## Release rule (directive 2026-11-01)

For each release, test BOTH builds: the debug build (no
`SPECIALTYCIRCUITS_RELEASE`) and the release build (flag set). Both must
pass the acoustic gate (rig mix + mute), and both must be built from a clean
repo state — no uncommitted changes at build time. The release build is what
ships (`release/` + tag); the debug build proves the instrumentation stays
functional and that gating it does not perturb audio.

## No DAC channel mixing (owner directive 2026-11-05)

Nothing in this project touches the ES9219Q channel-mixing or automute
registers. `0x02` carries `CH1_MIX_SEL`/`CH2_MIX_SEL`, which route one I2S slot
to both DAC channels; a codec told to do that reproduces the right channel on
both outputs and goes silent on a left-only stream, while every host control
still reads nominal. The former writes (`0x02` with `MIX_SEL_DEFAULTS`, `0x04`
automute time, `0x05` automute level) are removed rather than left commented,
the value literals are gone from `es9219q.h`, and `csd_diag_reg_write()` refuses
`0x02`, `0x04`, `0x05` and `0xC0` (`0xC0` is SOFT_RESET: this build has no codec
power-on-reset path, so a soft reset that does not come back needs the supply
pulled). Reads stay open — refusing them would hide the state we need to see.

The chip runs these registers on its own reset defaults. Measured boot values on
this board: `0x02=0x34`, `0x0D=0x40`. Do not assume the datasheet's `0x04`.

## Dependency pinning: lib_xua is a separate repo

`lib_xua` is NOT a gitlink inside this app tree. Any commit that changes
behaviour must therefore also commit the `lib_xua` repo and record its SHA, or
the build is not reproducible and the "whole dependency tree" is not committed.
Current pin: branch `csd-121005`, commit `77245f99` (its parent `021c97ae`
restores the loopback tap hooks). Keep `I2S_LOOPBACK_HOOK.md` in step with it.

## Docs index

- [`TEST.md`](TEST.md) — every acceptance case: criterion, measurement method,
  premises, known limitations, result semantics, current measured status.
- [`FLASH_AND_RECOVERY.md`](FLASH_AND_RECOVERY.md) — the two flash paths, JTAG
  recovery, archive-before-flash.
- [`ACOUSTIC_LATCH_FINDINGS.md`](ACOUSTIC_LATCH_FINDINGS.md) — the measured
  channel-collapse latch: symptoms, what was ruled out, state A vs state B.
- [`I2S_LOOPBACK_HOOK.md`](I2S_LOOPBACK_HOOK.md) — the pin-32 hardware
  loopback plan, with the pin map and why `I2S_WIRES_ADC` must not be used.

## Measurement integrity

- Bit-perfect results are only valid on a **flashed** image that boots by itself.
  Under `xrun --io --xscope` the loopback path measured 0.0007–0.0033 % on a data
  path that is bit-perfect when flashed (100.0000 %). Debug loads are for
  bring-up, not for accept/reject.
- Prove which image answered before trusting a result: read the `*IDN?` stamp.
  A failed flash returns a stale image, and a wrapper that ends on `echo` masks
  `xflash`'s exit status — check `Site 0 has finished successfully`.
- Prefer raw counts over ratios when reporting: differing bits, zero words,
  slips, frames. A percentage hides which of those happened.

## TODO

Feature list lives in [`TODO.md`](TODO.md). Owner-directive items land there;
one commit per item via ub-mode, acoustic gate (rig mix + mute) after each.

## Merge notes (2026-11-01, mvp → main, commit 71c79b0)

- Merge of `origin/mvp` (16de3fa): pin configuration taken from mvp
  (`src/ClearSoundDac.xn`); main's copies preserved under `archive_revBB/`
  with `_revBB` suffix (board.xc, es9219q.h, ClearSoundDac.xn). `archive_revBB/`
  sits OUTSIDE `src/` because XMOS cmake globs every `*.xc` under `src/` and
  would double-compile the archived copies.
- es9219q.h resolution keeps main's datasheet-correct MUTE/UNMUTE pair
  (reg 7 bit0: 1=mute) — mvp carried the pre-fix inverted copy; callers in
  board.xc use these macros by name. mvp's real edit (FILTER_SHAPE 1→0) is
  already in main's copy.
- **PIN-MAP / BOARD-REV WARN:** mvp's `.xn` rotates I2S/DSD pins
  (BCLK/LRCLK/DATA across XS1_PORT_1A/1B/1C) relative to main's. The bench
  board (XK-EVK-XU316 rig) is wired for MAIN's map: with the committed
  mvp-map `.xn` the DAC outputs silence (~−87 dBFS) although firmware boots
  and SCPI answers. The merged firmware was gate-verified (4→2 mix PASS,
  mute 65.8 dB) built with `archive_revBB/ClearSoundDac_revBB.xn`. Build
  against the mvp map only for the board revision it targets.
