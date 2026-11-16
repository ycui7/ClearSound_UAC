# CSD UAC2 DAC — Relay Release 2026-11-16

Audio path = guaranteed bit-perfect relay. Upsampler and DSP are permanently off:
arming inputs are pinned at compile time; the normal (certified pass-mode) code
bodies all run unchanged.

## Artifacts
- Image: `csd-relay-release-2026-11-16.xe`
  - md5 `f42d2e7b95e6e54d85965577ee0b0947`
  - sha256 `7cfe3c6e028a71c2bc9d4051e7b97d1c6036df6e60a0a978715918768c82c6e9`
  - size 5,275,628 B
- Build id (reported by `*IDN?`): **`2026.11.16.7-67d107b`** — clean commit hash, no `-dirty`
- Commit: `67d107b0edd1...` ("release: CSD_RELEASE relay guard, arming inputs pinned only"), tag `csd-relay-release-2026-11-16`
- Supersedes `d97c4a8e373f83d60afb0215245cf849` (build `2026.11.16.4-58b386c-dirty`):
  same compiled source (that build was HEAD `58b386c` + the guard as uncommitted edits,
  later committed as `67d107b`), rebuilt from the clean commit so the stamp is honest.
  The full gate suite below was run on the `d97c4a8e` build of this same tree.

## How to build
```
EXTRA_FLAGS="-DCSD_RELEASE=1" TREE=<wt path> bash ~/.hermes/cache/scratch/build_tree.sh
```
(CMake; `CSD_RELEASE` default OFF — dev builds are textually unchanged; guard is 2 files, 3 hunks.)

## Mechanism
- `csd_ups_block.c`: `get_want/active/armed` → 0; `set_want/set_enabled` force the
  argument to 0 then run their normal bookkeeping. `note_stream_rate`,
  `note_wire_rate`, `csd_ups_feed`, pop path: unguarded, full certified code.
- `csd_dsp.xc`: untouched (preset 0 + armed 0 = certified byte-exact bypass).
- `csd_diag_scpi.c`: `DIAG:UPSAMPLing:STATe` / `DIAG:DSP:PRESet` writes validate,
  accept-and-discard; queries answer the truth.
- No blocking call was removed. Note on the first attempt: a full-stub guard variant
  coincided with a dead SCPI transport on the bench; bisect builds with the identical
  stub sets later booted with `*IDN?` alive twice each, so that death was not
  attributable to the guards (environmental; recorded honestly in the commit message).

## Gates — all PASS (image d97c4a8e, one cold boot per rate, first stream at that rate)
| Gate | Result |
|---|---|
| SCPI/INFO alive | `*IDN?` after every boot; 181 INFO fields; armed/enabled/want 0; blocks/pops/reps/push_over/tap_fires 0 over 2 s |
| Relay 384k | 6,095,284/6,095,284 = **1.0000000** (d=−1004) |
| Relay 192k | 3,033,987/3,033,987 = **1.0000000** (d=−14157) |
| Relay 96k | 1,517,650/1,517,650 = **1.0000000** (d=−6423) |
| Relay 48k | 758,750/758,750 = **1.0000000** (d=−3286) |
| FFT per fs | CLEAN ×4: 384k −170.5 · 192k −164.2 · 96k −153.8 · 48k −147.6 dB (G2 maxrun 1) |
| Repeat-boot identity 192k | 1400/1400 anchors; payload24 1.000000; raw32 1.000000 |
| Arming refused (48k) | STATe 1 + PRESet 2 accepted, error queue empty, armed/want/blocks stay 0, `*IDN?` ×5, relay still 758,799/758,799 = 1.0000000, no wedge |

Zero-frame exclusions: exactly one head run per capture equal to the played 0.85 s
lead-in zeros (run length == −d, proven per tag). No other zeros. low8 non-zero:
L 0, R 0. Zero real mismatches.

## Gates — HYBRID re-cert all PASS (image `4560ae64` = `16e`, one cold boot per rate, 2026-11-16)
The "do NOT reuse linear-phase numbers" instruction is discharged: the suite was
re-run cold per rate on the hybrid fleet image. `DIAG:FILTer:STATe?` (tile-0 owner
mirror — I2C readback is dead) answered HYBRID on all 5 boots; stamp
`2026.11.16.13-6aec215` on every boot. Harness: `scratch/hybgate_all.sh`
(capv4_pass 16 s pattern → pass_verify → mic_tone_fft), log `/tmp/hybgate.log`.

| Gate | Result |
|---|---|
| Relay 384k | 6,095,278/6,095,278 = **1.0000000** (d=−1005), low8 L0 R0 |
| Relay 192k | 3,034,366/3,034,366 = **1.0000000** (d=−13776), low8 L0 R0 |
| Relay 192k (2nd boot) | 3,035,323/3,035,323 = **1.0000000** (d=−12818), low8 L0 R0 |
| Relay 96k | 1,517,456/1,517,456 = **1.0000000** (d=−6615), low8 L0 R0 |
| Relay 48k | 758,702/758,702 = **1.0000000** (d=−3334), low8 L0 R0 |
| FFT per fs | CLEAN ×4 (48/96/192/384k), PAC pop/pick counters all 0 |
| Repeat-boot identity 192k | rigid-offset-aligned: **0 differing frames / 3,034,366** (payload24 and raw32 word-exact); naive frame-0 compare shifts by the boot head-offset delta (958 frames) — align content starts first |
| Arming refused (48k) | `DIAG:STATe 1` + `DIAG:UPSAMPLing:PRESet 2` accepted; `DIAG:UPSAMPLing:INFO?` want/enabled/armed = 0,0,0; `*IDN?` ×5 alive, no wedge |


## Filter variants (reg 7 [7:5], ES9219Q_FILTER_SHAPE)
| File | Filter | Build id | md5 |
|---|---|---|---|
| `csd-relay-release-2026-11-16-linearphase.xe` | linear phase fast roll-off (`000`, default; group delay 35/fs) | `2026.11.16.7-67d107b` | `f42d2e7b95e6e54d85965577ee0b0947` |
| `csd-relay-release-2026-11-16.xe` | same as above (canonical release name) | `2026.11.16.7-67d107b` | `f42d2e7b95e6e54d85965577ee0b0947` |
| `csd-relay-release-2026-11-16-hybrid.xe` | hybrid fast roll-off (`110`; group delay 18.5/fs, pre-ring suppressed, −3 dB at 0.430·fs) | `2026.11.16.8-3ec09fa` | `070467f36c5c9a1505556528985eb925`, sha256 `c7a8fa59…cbefc`, 5,275,268 B |
| `csd-relay-2026-11-16d-hybrid-btnftr.xe` | hybrid BOOT default + **two-button hold toggles the FIR at runtime** (hold VOL UP+DOWN >1 s; SCPI `DIAG:FILTer:STATe?`/`DIAG:FILTer:TOGgle`) | `2026.11.16.11-f024130` | `82bcafdab2e75f1956ca7665803a9615` |
| `csd-relay-2026-11-16e-hybrid-btnftr-led.xe` | Adds D2-blue 1 Hz flash while linear phase enabled (lowest display priority; mute/error display untouched). | `6aec215` | `4560ae6476256deda57d426fed464dc1` |

The `16b`/`16c` btnftr images are **withdrawn** (md5s `0bb62c21…`, `93afccb6…`,
deleted). `16c` additionally carried a broken `DIAG:FILTer:STATe?`: the ES9219Q
I2C READBACK PATH IS DEAD on this board -- every register, CHIP_ID included,
answers 0x00 (measured 2026-11-16, both bench units). `16d` reports the FIR
state from the tile-0 owner (s_filter_bits) instead; boot=HYBRID and the
TOGgle LINEAR/HYBRID round trip verified on 8c5357f6. All historical
`REGister:READ?` "verifications" (the 0x60 hybrid check included) were noise.
Original `16b` defects:
the full-loop xsim sim (`wt/tools/btnxsim/btn_deglitch_sim.xc`, 5/5 PASS)
found two defects in it — (1) a single confirmed press could emit **two**
volume steps (double-confirm of the same press stepped on every level-1
window; fixed by gating I1 on a 0→1 confirmed transition), (2) the combo
hold was **5 s, not 1 s** (`100 × 50 ms`; fixed to `20 ×`). Fleet units are
still on the `16` hybrid build without the button feature; flash `16c` to
enable it.

The gate suite below was certified on the LINEAR-PHASE build. The hybrid build
changes the codec's 8× FIR, which the analog loopback sees: do NOT reuse the
linear-phase pass-sheet numbers — re-run the gates and read back reg 7 (expect
0x60) before certifying hybrid. **DISCHARGED 2026-11-16: hybrid re-cert above
(image `4560ae64`, 16e) — all gates PASS. Reg-7 readback expectation superseded:
I2C readback is dead; `DIAG:FILTer:STATe?` owner mirror answered HYBRID on all
five boots.**

## Legacy POR release
| File | Description | Commit | md5 |
|---|---|---|---|
| `csd-legacy-POR-original.xe` | **Canonical** legacy power-on-reset factory image — the user-provided ORIGINAL factory .xe (config `2AMi2o2xxxxxd`, "XMOS (UAC2.0)", serial `123456789`, built Windows `C:/SpecialtyCircuits/usb_audio_sc`, XTC 15.3.1; 3.0 MB). Tag `legacy-POR-release` → `094dad0b`. DFU bin: `csd-dfu-upgrade-legacy-POR-original.bin`. | `094dad0b` | `d1fbea85d82ebbe1de7b51c5db5dd02c` |

The canonical image is the ORIGINAL factory binary, not a rebuild. Five
source-reconstruction attempts (era module pins, config `2AMi2o2xxxxxd`,
lib_xua_mod fork `1b824ca1`) were all retired — see `release/RELEASE-legacy-POR.md`
for the forensic recipe and the honest record. NOTE: this bare-POR app does not
run on the bench unit at bus 5-2 (hardware fault: ep0 real-time failures, device
crashes off-bus; that unit was XTAG-recovered to the modern 16e image).
Factory-flash with: `xflash -f --boot-partition-size 1048576 --factory <xe> --upgrade 1 <xe> --data bootFlash trim_journal_erased_4k.bin`.

## Flash
```
bash ~/.hermes/skills/clearsound-dac-xmos/scripts/flash_verified.sh <ABS path to .xe> f42d2e7b95e6e54d85965577ee0b0947
sudo -n modprobe -r snd-usb-audio; sleep 2; sudo -n modprobe snd-usb-audio
```
Or via DFU (upgrade partition, boot partition untouched): xflash -o upgrade bin +
tools/dfu_flash.py serial-pinned; the unit parks in dfuIDLE after manifest — a
follow-up explicit bus reset returns it to runtime (proven 2026-11-16 on f30aeb64
and 54104e22).

## Related
- Armed-DSP certification (separate path): `../docs/bitperfect_gate_pass_sheet_2026-11-15.md`
- Dev cert image (armed path): `cand_bp31_bb8cee6b.xe`, md5 `bb8cee6bf2b5fc670d52f1f299d1ef7b`
