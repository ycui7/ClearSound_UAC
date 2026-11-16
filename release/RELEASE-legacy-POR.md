# RELEASE — Legacy POR (094dad0b)

Factory power-on-reset image, built 2026-11-16. Tag: `legacy-POR-release` → `094dad0b`
("default FIR to linear phase slow roll off", 2025-07-10 — pre-USB-refactor baseline).

| File | md5 | Notes |
|---|---|---|
| `csd-legacy-POR-094dad0b.xe` | `273673351e659d59ad79719e6184c8b3` | Factory image, 3,654,296 B (era DSD config) |
| `csd-dfu-upgrade-legacy-POR-094dad0b.bin` | `302c6a86a25186004e116f27232b3d1b` | DFU upgrade slot-1 bin |

## Build provenance
- Toolchain XTC 15.3.1 (user directive 2026-11-16), build system xcommon_cmake.
- FIRST CUT RETIRED (md5 0d553569/684844b9): that build used the modern-tree
  CMakeLists + campaign flags (NATIVE_DSD, DSD_CHANS_DAC=2, UAC_FORCE_FEEDBACK,
  campaign config 2AMi2o2xxxxxd), un-pinned module HEADs, and xcommon_cmake
  silently FetchContent-downloaded any missing module from GitHub at HEAD.
  User caught it ("did you get all dependency to the right time?") — no.
- Isolated sandbox `~/sw-audio-sc/por094sb`: app worktree at `094dad0b` under `sb/`
  (xcommon resolves the sandbox as `app/../..` — depth matters), `sb/deps.cmake` =
  era Makefile set (lib_xua, lib_i2c, lib_sw_pll; the app's relative
  `../../../shared/version.h` include is served by `sb/shared` -> era sw_usb_audio
  worktree `57ffd61`), and ALL fifteen module worktrees era-detached (<=2025-07-11):
  lib_xua c4ea3f59, lib_i2c 5757817, lib_sw_pll 8e629ba, lib_adat 5b52c5e,
  lib_locks 3076f7f, lib_logging b8e939c, lib_mic_array 8620978, lib_spdif 9bfeff3,
  lib_xassert c8b59c9, lib_xcore_math 175dd19, lib_xud cd126400,
  lib_audio_dsp d9c15049, lib_board_support 921c3a1, lib_i2s 2cae684, libscpi 698eabe.
- Build config = the ERA-COMMITTED `CMakeLists.txt` at 094dad0b (byte-identical to
  git blob 37deca90; config `2AMi2o2xxxxxd` incl. era NATIVE_DSD/DSD_CHANS_DAC/
  UAC_FORCE_FEEDBACK -- the user's actual July build path; build_run.bat ran
  bin\2AMi2o2xxxxxd). deps.cmake = lib_xua, lib_i2c, lib_sw_pll: the era app src
  `git grep`s clean of scpi/audio_dsp/board_support/i2s (libscpi cmake integration
  is only from 2026-10-30 -- provably not July 2025). Sole extra define:
  -DSPECIALTYCIRCUITS_RELEASE (factory identity, in the build lineage since
  2025-01-25; field POR units enumerated ClearSound DAC (UAC2.0)/UHA3-000-001).
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
