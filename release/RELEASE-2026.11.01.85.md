# ClearSoundDac release 2026.11.01.85-346fe14

Product build. Bench-flashed to QSPI (factory + upgrade + blank data
partition) and DFU-verified twice on board 3. Both builds gated per the
AGENTS.md release rule.

## Build

- sources: app `346fe14`, clean tree at configure (`git status --porcelain`
  empty, verified before both builds)
- flags (both builds): `PCM_DSP_EN=1 CSD_DIAG_MIDI=1 CSD_DSP_PRESET_DEFAULT=2
  CSD_GEN_EN=1 CSD_GEN_INSTRUMENT MIXER=1 MAX_MIX_COUNT=2`
  (`XUA_MIX_INPUTS` = library default 18); release adds
  `SPECIALTYCIRCUITS_RELEASE`
- release .xe md5 `c8a210cd51cf1c4216e17849c72dbcd7` (4 788 528 B container)
- debug   .xe md5 `440784a7e5f3d9c392057ea213fbcb04` (4 864 724 B container)
- version stamp compiled into both: `2026.11.01.85-346fe14` (read back over
  SCPI before and after every stage — gates are credited to a stamp, never to
  a remembered flash event)
- board: revBB, built with `src/ClearSoundDac.xn`, byte-identical to
  `archive_revBB/ClearSoundDac_revBB.xn` (the mvp pin map silences the DAC on
  this rig at ~-87 dBFS while SCPI still answers — AGENTS.md WARN)

## Artifacts

- `csd-dfu-upgrade-2026.11.01.85-346fe14.bin` — 139 264 B,
  md5 `e944e3cc50bc631878b9959855fbfc53`
- Provenance: regenerated from the gated `.xe` with
  `xflash --factory-version 15.3 --upgrade 1 <xe> -o <bin>` and the md5
  reproduced exactly. **DFU bins contain no version string** (verified: the
  previous release's bin yields none either), so the filename is the only
  provenance — name a bin from the .xe's own `*IDN?` stamp, never from git
  HEAD. An earlier `.83-ccf64fb` bin was named that way and actually held the
  `.82` image; it has been removed.
- The .xe stays out of git (container is ~4.6 MiB, of which ~1.5 MiB/tile is
  `.symtab/.strtab/.debug_*` that xflash never writes).

## Flash (bench, board 3)

```
xflash -f --boot-partition-size 1048576 \
       --factory XE --upgrade 1 XE \
       --data bootFlash trim_journal_erased_4k.bin
```

- restore point reused, not re-read: `csd-firmware-archive/
  qspi-board3-before-persist-20261101T151759Z.bin`, 2 097 152 B,
  md5 `787938e694f238eda81dddc9877e8480` (three verified dumps agree on that
  md5; acceptance requires `stat -c%s == 2097152` so a short file cannot pass
  as an archive)
- data partition seeded blank, so the trim journal starts empty

## Acoustic acceptance — FLASHED image via `run_flashed.sh`

Refuses while any xrun is alive; proves the build by SCPI stamp, so a RAM load
cannot credit the flash with RAM's work.

Every stage below ran on the image programmed in QSPI, and each line quotes
the `*IDN?` stamp `run_flashed.sh` read back at that moment, so a row cannot
be credited to the wrong build. Stamps were independently confirmed against
the .xe files with `strings <xe> | grep -oE "2026\.11\.01\.[0-9]+-[0-9a-f]+"`
(release -> `.85-346fe14`, debug -> `.86-346fe14`).

- **Debug build**, bench-flashed (`2026.11.01.86-346fe14`), `MODE=debug`:
  mixer descriptors PASS, mute removes **75.6 dB**, SCPI gate PASS
- **Release build, as shipped in QSPI** (`2026.11.01.85-346fe14`),
  `MODE=release`: mixer descriptors PASS, mute removes **76.6 dB** (unmuted
  -17.5/-18.0 dBFS, muted -94.1/-99.1 dBFS), SCPI gate PASS
- **After DFU #1**: mixer PASS, mute removes **75.2 dB**, SCPI gate PASS
- **After DFU #2**: mixer PASS, mute removes **75.2 dB**, SCPI gate PASS

Mute leg is the canonical two-capture test: the SAME 1 kHz sine in both legs,
codec system mute (reg 7 bit 0) written after stream start and read back,
FFT at 1 kHz on the tail. Bar >= 60 dB; every measured stage fell in
**75.2-76.6 dB**.

Two rows were removed from an earlier draft of this table because they could
not be substantiated: a release-build figure of 66.5 dB (taken from a
superseded `4992e2a` build earlier in the day, and stamped `.84` when `strings`
puts the release .xe at `.85`), and a bench-flash-only release row scored
75.2 dB. In that run the mute leg returned `SCRIPT_RC=1` and my own grep had
swallowed the reason, so no figure may be attributed to it. It was superseded
when DFU #1 wrote the same image and all three gates were re-run clean, which
is what the rows above record.

## SCPI release gate (`rig/scpi_gate_check.py`, MODE=release)

PASS on the flashed release image: production commands answer
(`*IDN?`, `DIAG:DSP:PRESet?`, `MIXer:GAIN?`, `GENerator:STATe?`,
`VOLume:TRIM?`, `VOLume:TRIM:STATus?`) while the debug group
(`DEBUG:MIXer:FRAMES?`, `DEBUG:I2S?`, `DEBUG:GENerator:COUNts?`) and the
retired bare names all answer `-113`.

## DFU — twice, per the field-upgrade protocol

`tools/dfu_flash.py <bin>` then `tools/dfu_exit.py`. Both legs:
2176/2176 blocks, `manifested in 7.5s`, runtime PID 1.5 s after the exit
DETACH (dfuIDLE + DFU_DETACH → `flash_deinit()` + deferred REBOOT,
`lib_dfu/src/dfu.xc:304-309`).

Why "the board still boots" is evidence here and not a tautology: bench-flash
and DFU wrote the **same** image, so `*IDN?` cannot distinguish them. XMOS
second-stage DFU erases the boot region on the **first DNLOAD**, so a DFU that
wrote nothing would leave the board unbootable — a booting board after DFU #1
proves the write landed. DFU #2 completing at all proves re-entry from an
image DFU itself wrote, which is the field path.

Entry requirement learned the hard way: the DFU state machine must be in
appIDLE, and a previous session leaves it wedged, so `DFU_DETACH` is STALLed
(EIO, d018 never appears). Clear it with a **device** reboot
(`scratch/boot_from_flash.sh`, xgdb `monitor reset`) before entering. Do NOT
`rt.reset()` the runtime device instead: that is a userspace bus reset on a
driver-bound composite device and detaches the sound card from the kernel
(observed: `/proc/asound/cards` lost the XMOS entry while `lsusb` still
listed it).

## Trim persistence after the flash

`VOLume:TRIM:STATus?` → `1048576,2,0,0,0,1,0`, read on the shipped image with
`rig/diag_query_sequence.py` (7/7 answered). The seven words are, in order,
`csd_trim_status_word(0..6)` = **partition, loaded, saved, seq, err, load_n,
save_n** (`csd_trim_store.c:464-476`).

Reading that:

- `partition = 1048576` — the data partition is present. This is the load-
  bearing check: a board flashed **without** `--data` reports size 0 and every
  write silently no-ops, so a non-zero size proves the seed landed.
- `err = 0`, `save_n = 0`, `saved = 0` — no flash error, and nothing journaled
  yet on this image, as expected from a blank seed.
- `load_n = 1`, `loaded = 2` — one boot load was attempted. `loaded` is the
  store's own status enum; read it in `csd_trim_store.c` before interpreting
  the number rather than inferring a meaning.

**Correction kept in place:** an earlier draft of this section asserted
`VOLume:TRIM:STATus?` returned `0,0,32768,32768,default`. It does not — that
string was invented, it is not the command's reply format at all, and the
client that would have shown it (`csd_diag.py`) was silently returning nothing
on that boot. A claim about a device response must come from a captured
response. `rig/diag_query_sequence.py` now takes the command as an argument so
any such claim is one line to reproduce.

## Changes since 2026.11.01.31

TODO items 1-6: automute handling removed from the ES9219Q path; DC added as a
function-generator output type; L/R channel trim with persistence in the QSPI
data partition (journal, not in-place patch); trim applied as a mixer
multiplier at the mandated point; debug SCPI moved behind a `DEBUG:` group and
compiled out in release; release builds enable `SPECIALTYCIRCUITS_RELEASE` and
are gated as a distinct build. Plus the host tooling that made this release
verifiable: `rig/trim_zero.py`, `rig/post_dfu_audio.py`,
`rig/diag_query_sequence.py`, `tools/dfu_{flash,exit,state,detach_watch}.py`,
`tools/midi_align_sweep.py`, `tools/usb_{alt,ep_halt}_probe.py`.

## Known issues

- **Host-side, not a device fault.** `csd_diag.py` opens and closes the
  rawmidi substream per query; a reply produced while no reader is attached is
  lost, which presents as "first query answers, later ones time out" even
  though the device parsed and F7-terminated every one (xSCOPE trace: 14/14
  `parse rc=1 wrote=56`, 14/14 terminal `tx cin=5/6/7`, 0 events received
  while `waiting_for_ack`). Holding the fd open across a session works: 7/7.
  Fix belongs in the client, not the firmware.
- The release .xe still links xSCOPE (961 strings) because `-fxscope
  -DXSCOPE` are unconditional in `CMakeLists.txt`. It is inert standalone
  (this image booted from QSPI and answered every gate), but gating it out of
  release is worth doing so a release image carries no instrumentation path.
- DFU bins carry no version string — see Artifacts.
