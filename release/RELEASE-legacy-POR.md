# RELEASE — Legacy POR (original factory image)

Canonical power-on-reset factory firmware. Tag: `legacy-POR-release` → `094dad0b`
("default FIR to linear phase slow roll off", 2025-07-10 — pre-USB-refactor baseline).

| File | md5 | Notes |
|---|---|---|
| `csd-legacy-POR-original.xe` | `d1fbea85d82ebbe1de7b51c5db5dd02c` | THE original factory image as built on the original PC (XTC 15.3.1, Windows, `C:/SpecialtyCircuits/usb_audio_sc`). User-provided 2026-11-16; hardware-verified working. |
| `csd-dfu-upgrade-legacy-POR-original.bin` | `6f7cfaa5914db24a57a4047159a471fa` | DFU slot-1 upgrade bin (98,304 B) packaged from the original .xe — canonical POR restore image. |

POR behavior: enumerates `20b1:0018` "XMOS (UAC2.0)" serial `123456789` (no
SPECIALTYCIRCUITS identity), DSD off, MIDI off, SPDIF-RX compiled, DFU runtime
present (DFU-upgradable both directions). Note: one bench unit has a hardware fault
that prevents the legacy POR build from running — recover to latest fleet firmware
there rather than debugging POR.

## Provenance of the original (forensics, 2026-11-16)
Embedded compiler-diagnostics transcript in the .xe fingerprints the exact era tree:
- lib_xua = **upstream `c4ea3f59`** (ep0 `%s`-ambiguous diag lines 144/181/228/260;
  the `1b824ca1` user-fork stack shifts them +17 — the lib_xua_mod fork was never
  in POR). Fork survives in `~/workspace/xmos-csd-backup/lib_xua-1026-2223.bundle`.
- lib_i2c = `26c3cce0` (release_6_4_0; 4-file line fingerprint).
- lib_spdif compiled (`XUA_SPDIF_RX_EN` default), DFU on (`XUA_DFU_EN` default 1).
- Flags = era Makefile BUILD_FLAGS only (uncommitted Windows-Makefile config
  `2AMi2o2xxxxxd`); the committed era CMakeLists DSD/feedback flags were NOT used.
- Toolchain XTC 15.3.1.

All reconstruction attempts built from source in this session (0d553569, 50c5c9a4,
8baa2723, 35b307db, 27367335) are retired and deleted — see LOG.md late-8..late-11
for what each got wrong. If a source-rebuild is ever needed, this file plus those
log entries define the recipe.

## Factory flash (blank board)
```sh
cd ~/XMOS-15.3.1 && set +u && . ./SetEnv && set -u
export PATH="$HOME/XMOS-15.3.1/bin:$PATH"
xflash -f --boot-partition-size 1048576 \
  --factory  csd-legacy-POR-original.xe \
  --upgrade 1 csd-legacy-POR-original.xe \
  --data bootFlash ~/workspace/csd-firmware-archive/trim_journal_erased_4k.bin
```
