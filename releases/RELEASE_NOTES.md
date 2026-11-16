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

## Filter variants (reg 7 [7:5], ES9219Q_FILTER_SHAPE)
| File | Filter | Build id | md5 |
|---|---|---|---|
| `csd-relay-release-2026-11-16-linearphase.xe` | linear phase fast roll-off (`000`, default; group delay 35/fs) | `2026.11.16.7-67d107b` | `f42d2e7b95e6e54d85965577ee0b0947` |
| `csd-relay-release-2026-11-16.xe` | same as above (canonical release name) | `2026.11.16.7-67d107b` | `f42d2e7b95e6e54d85965577ee0b0947` |
| `csd-relay-release-2026-11-16-hybrid.xe` | hybrid fast roll-off (`110`; group delay 18.5/fs, pre-ring suppressed, −3 dB at 0.430·fs) | `2026.11.16.8-3ec09fa` | `070467f36c5c9a1505556528985eb925`, sha256 `c7a8fa59…cbefc`, 5,275,268 B |

The gate suite below was certified on the LINEAR-PHASE build. The hybrid build
changes the codec's 8× FIR, which the analog loopback sees: do NOT reuse the
linear-phase pass-sheet numbers — re-run the gates and read back reg 7 (expect
0x60) before certifying hybrid.

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
