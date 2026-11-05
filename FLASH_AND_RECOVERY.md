# Flashing and recovery — ClearSound DAC (board `2AMi2o2xxxxxd`, serial `8c5357f6`)

## Standing authority (owner directive, 2026-11-05)

> "You can run DFU whenever you want, I do not want to supervise it. If it fails,
> you use JTAG to bring the board back."

Flashing is **pre-authorised**. Do not ask for supervision before a flash, and do
not stop half-way waiting for approval. The recovery route is JTAG, so a failed
flash is an inconvenience, not an outage. Two conditions still apply, because they
are what makes self-service flashing safe:

1. **Archive the boot flash before overwriting it.** Read the current QSPI image
   to a dated file first. This is proven practice here — earlier archives are
   named `qspi-board8c5357f6-before-<UTC>.bin`. A flash without an archive is not
   reversible even with JTAG, because the previous contents are gone.
2. **Prove which image is actually running after the flash.** The version stamp
   inside the `.xe` is the provenance; read `*IDN?` over MIDI and compare. A
   filename or a successful flash command proves nothing.

## Two ways to flash this USB DAC

### 1. Over the debug probe — `xflash` (bench, and the recovery path)

This is the proven route. `xflash` writes the boot flash (QSPI) directly and
takes the partition layout, so a full image needs the boot partition plus the
trim/persistence data partition. Proven form, quoted from
`release/RELEASE-2026.11.01.85.md`:

```sh
xflash -f --boot-partition-size 1048576 \
       ... \
       --data bootFlash trim_journal_erased_4k.bin
```

Release images are also produced through `xflash` for the factory/upgrade bins:

```sh
xflash --factory-version 15.3 --upgrade 1 <xe> -o <bin>
```

`xflash` lives in `~/XMOS/XTC/15.3.0/bin`. The board's known-good QSPI dump is
2 097 152 bytes; a read-back that is not that size means the flash geometry is
not what the build expects, and flashing should stop there rather than proceed.

### 2. Over USB — DFU (field path, no probe attached)

DFU is the path that must work for a field update: no debugger, no bench rig,
just the USB cable. **Host-side tooling is not yet verified on this machine** —
neither `xdfu` nor `dfu-util` is installed, and no DFU flash command is recorded
anywhere in this repo, so any command line in a doc or script claiming to do DFU
so far is unverified. First DFU run must therefore establish and record:

- how the board enters the DFU/bootloader state (and whether that survives a
  power cycle);
- the USB ID the device presents in that state, confirmed with `lsusb`;
- the exact flash command, and the erase/read-back that proves it landed.

Until that is recorded, do not treat "DFU flashing" as available. Case 5 of the
acceptance suite is the gate for it, and the owner's spec requires the DFU test to
run **twice** on the same image — a first pass can succeed while leaving DFU
itself damaged, so a single pass cannot certify the upgrade path.

### Not a flash: RAM load

Every bench gate in this project runs images loaded into RAM via `xrun`
(`rig/load_xe.sh`). That is how results are produced, and it deliberately is not
proof about the flash: a RAM image does not exercise boot, QSPI, or the
persistence partition. Any claim about a *flashed* build has to come from an
image read back over `*IDN?` after `xflash`.

## Recovery when a flash fails

`xgdb` (and `xgdbserver`) are present in `~/XMOS/XTC/15.3.0/bin`, and JTAG access
is the reason flashing is pre-authorised. Recovery sequence: attach over JTAG,
confirm the device is reachable, then re-program the boot flash from the archived
`.bin` — or from the last known-good release image, whose md5 is recorded in
`release/RELEASE-2026.11.01.85.md` (`c8a210cd51cf1c4216e17849c72dbcd7`).

Two facts worth knowing before a recovery, both learned the hard way here:

- Killing the `xrun` process does **not** stop an image already loaded on XS3; the
  device keeps running the RAM image. Reload, don't just kill.
- A board left in a bad state by repeated reloads is not evidence about firmware.
  Distinguish "this build is broken" from "the board has been poked for hours"
  before writing a verdict.
