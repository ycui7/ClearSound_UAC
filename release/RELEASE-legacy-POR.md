# RELEASE — Legacy POR (094dad0b)

Factory power-on-reset image, built 2026-11-16. Tag: `legacy-POR-release` → `094dad0b`
("default FIR to linear phase slow roll off", 2025-07-10 — pre-USB-refactor baseline).

| File | md5 | Notes |
|---|---|---|
| `csd-legacy-POR-094dad0b.xe` | `0d5535697feda5eda367d7002cb8419d` | Factory image, 3,654,296 B |
| `csd-dfu-upgrade-legacy-POR-094dad0b.bin` | `684844b9dc9e0e7a5f5d0ccc6aea36da` | DFU upgrade slot-1 bin |

## Build provenance
- Toolchain XTC 15.3.1, same pinned flag set as the campaign builds minus `CSD_*` flags.
- Isolated sandbox `~/sw-audio-sc/por094sb`: app worktree at `094dad0b` under `sb/`
  (xcommon resolves the sandbox as `app/../..` — depth matters), `deps.cmake` copied in,
  shared modules symlinked, `lib_xua` = worktree of era XMOS develop `c4ea3f59` (5.1.0).
- Two build shims, behavior-neutral, documented in `~/sw-audio-sc/LOG.md` (2026-11-16 late-3):
  1. `extern void process_xscope(chanend);` prototype in `lib_xua/src/core/main.xc`
     (carried by XTC 15.3.0, absent in 15.3.1).
  2. Restored `unsafe chanend uc_audiohw2;` in `clearsounddac_board.xc` (commented out at
     this WIP commit while `USER_MAIN_CORES` still assigns it → link error; the definition
     lived in the lost `lib_xua_mod` fork).
- Era tree predates `gen_version`: no build-id stamp inside the image; identity = commit + md5.

## Flash (blank / POR board)
```sh
cd ~/XMOS-15.3.1 && set +u && . ./SetEnv && set -u
export PATH="$HOME/XMOS-15.3.1/bin:$PATH"
xflash -f --boot-partition-size 1048576 \
  --factory  csd-legacy-POR-094dad0b.xe \
  --upgrade 1 csd-legacy-POR-094dad0b.xe \
  --data bootFlash ~/workspace/csd-firmware-archive/trim_journal_erased_4k.bin
```
