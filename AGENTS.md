# AGENTS.md — ClearSoundDac firmware

App repo for the CSD USB DAC (XU316 + ES9219Q). Build/ops procedure lives in
the `clearsound-dac-xmos` skill; action log in `../LOG.md` (ub-mode).
Release: `release/` + tag. Current: `v2026.11.01.85` (app 302bb22, DFU bin
md5 e944e3cc50bc631878b9959855fbfc53). Previous: `v2026.11.01.31`.

## Release rule (directive 2026-11-01)

For each release, test BOTH builds: the debug build (no
`SPECIALTYCIRCUITS_RELEASE`) and the release build (flag set). Both must
pass the acoustic gate (rig mix + mute), and both must be built from a clean
repo state — no uncommitted changes at build time. The release build is what
ships (`release/` + tag); the debug build proves the instrumentation stays
functional and that gating it does not perturb audio.

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
