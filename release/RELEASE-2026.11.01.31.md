# ClearSoundDac release 2026.11.01.31-5bb7e76

Product build, flashed to QSPI (factory + upgrade image) and DFU-verified.

## Build
- sources: app `5bb7e76`, lib_xua `e90de7f0` (clean tree at configure)
- flags: PCM_DSP_EN=1 CSD_DIAG_MIDI=1 CSD_DSP_PRESET_DEFAULT=2
  CSD_DSP_DIAG=1 CSD_DSP_DIAG_BUMP CSD_DSP_DIAG_LOOP CSD_GEN_EN=1
  MIXER=1 MAX_MIX_COUNT=2 (XUA_MIX_INPUTS = library default 18)
- .xe md5 396dc606e05f773a9ec9151c9ce54aa8 (4.58 MiB container; ~1.5 MiB is
  .symtab/.strtab/.debug_* that xflash never writes)
- flash image 143 360 B; boot partition 1 MiB (flash.bat); factory +
  upgrade(1) = 288 KiB used

## Flash (Linux translation of flash.bat / erase_flash_factory.bat / dfu_upgrade.bat)
- xflash -f --boot-partition-size 1048576 --factory XE --upgrade 1 XE
- DFU bin: xflash --factory-version 15.3 --upgrade 1 XE -o bin (139 264 B,
  md5 9d341848a37fac309f4ff46e44b1ec6d) + .ppb
- pre-flash QSPI archive (restore image): ~/workspace/csd-firmware-archive/
  qspi-before-release-full.bin, 2 MiB, md5 c0ca1033c0feb7d61210653f2e0317a5
- artifacts kept in ~/workspace/csd-firmware-archive/ (NOT committed)

## Acoustic acceptance (all on the FLASHED image, run_flashed.sh)
| stage                    | mix 4->2          | mute effectiveness |
|--------------------------|-------------------|--------------------|
| after xflash             | PASS              | 65.0 dB removed    |
| after DFU #1 (same fw)   | PASS              | 66.1 dB removed    |
| after DFU #2 (same fw)   | PASS              | 68.7 dB removed    |

mix legs: baseline silent ~-82; gen-only -18.4; host-only -17.8; both -12.3.
mute legs: unmuted -14.9/-15.4 dBFS 1 kHz; muted ~-80; gate >= 60 dB.

## DFU proof (user: same firmware twice)
upgrade #1 wrote the DFU bin over the xflash'd image, board reset, IDN
2026.11.01.31-5bb7e76, sound PASS. upgrade #2 wrote the SAME bin again over
the DFU-written image: entering DFU from a DFU-written image is what this
proves (factory loader path intact).
