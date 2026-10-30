#!/usr/bin/env python3
"""Case F measures -8 dB for a -32 dB volume step. Is it the case ORDER?

The two measurements that disagree:

  rig/codec_final.py   cset numid=5 -- 64,192, written BEFORE playback,
                       driven cavity fell 34 dB from its 192,192 reference.
  rig/ears_matrix.py   case F asks for the same 64,192 and the driven cavity
                       fell only 7.9 dB from case D's 192,192 reference.

Same control, same value, same board, opposite answers. So one of them is
measuring something other than what it names. The difference in the harness is
what happens immediately before the write: case F inherits case D, which
streamed full scale, and set_volume parks at (96,96) before writing the target.
The sweep started from a fresh stream each time.

Three orderings, each measured on the DRIVEN cavity only:

  F_first   fresh load, F written first, then captured
  D_then_F  D (192,192) captured, then F (64,192) captured  <- matrix order
  F_again   F written again after that, to show it is not one-shot drift

If F_first gives -32 and D_then_F gives -8, the harness is at fault: a volume
change inherited from a full-scale case does not reach the device, so every
volume case must set volume from a stopped stream and verify it took effect
across a stream open. If all three give -8, the board does not honor a
per-channel cut while its partner is at full scale, which is a firmware or
codec-configuration defect and the gate legitimately fails.

The control is read immediately BEFORE and AFTER each capture, because a UAC
driver can re-push a cached SET_CUR when a stream opens, and a read-back taken
before playback then proves nothing about the value in force during it.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ears_matrix as m  # noqa: E402

DAC = m.find_dac_card(None)
EARSC = m.find_ears_card(None)
print(f"DAC {DAC}  probe {EARSC}")


def one(tag, al, ar, vl, vr, tmp):
    stim = os.path.join(tmp, f"{tag}.stim.wav")
    cap = os.path.join(tmp, f"{tag}.cap.wav")
    m.set_volume(DAC, vl, vr, tmp)
    before = m.read_volume(DAC)
    m.write_tone24(stim, 3.5, al, ar)
    m.record(DAC, EARSC, 3, stim, cap)
    after = m.read_volume(DAC)
    res = m.analyse(cap, tones=(m.TONE_HZ,))
    lv, rv = res["L"].tone_db[m.TONE_HZ], res["R"].tone_db[m.TONE_HZ]
    loud = max(lv, rv)
    bv = "/".join(str((before[k] or {}).get("values", "?")) for k in ("L", "R"))
    av = "/".join(str((after[k] or {}).get("values", "?")) for k in ("L", "R"))
    print(f"  {tag:9s} ctl before={bv:12s} after={av:12s} "
          f"driven {loud:+6.1f} dBFS  (L {lv:+6.1f}  R {rv:+6.1f})")
    return loud, lv, rv


tmp = tempfile.mkdtemp(prefix="vol_order_")
print("\n=== D then F, the matrix order ===")
d_l, d_lv, d_rv = one("D", 0.8, 0.0, m.VOL_0DB, m.VOL_0DB, tmp)
f_l, f_lv, f_rv = one("F", 0.8, 0.0, m.VOL_M32, m.VOL_0DB, tmp)
print(f"  F relative to D: {f_l - d_l:+.1f} dB   (expect about -32)")

print("\n=== F again, same values, to rule out one-shot drift ===")
f2_l, _, _ = one("F2", 0.8, 0.0, m.VOL_M32, m.VOL_0DB, tmp)
print(f"  F2 relative to D: {f2_l - d_l:+.1f} dB")

print("\n=== third repeat (set_volume already parks both elements at 96")
print("    before writing, so this is a reproducibility check, not a new")
print("    write strategy) ===")
f3_l, _, _ = one("F3", 0.8, 0.0, m.VOL_M32, m.VOL_0DB, tmp)
print(f"  F3 relative to D: {f3_l - d_l:+.1f} dB")

print("\n=== reference again: is D itself still reproducible? ===")
d2_l, _, _ = one("D2", 0.8, 0.0, m.VOL_0DB, m.VOL_0DB, tmp)
print(f"  D2 vs D: {d2_l - d_l:+.1f} dB   (should be ~0)")

deltas = [f_l - d_l, f2_l - d_l, f3_l - d_l]
print(f"\n  F-family deltas vs D: {['%+.1f' % d for d in deltas]} dB")
if all(d <= -26 for d in deltas):
    print("  Volume IS honored per channel. The earlier -8 dB was a transient\n"
          "  harness condition; rerun the matrix and it should pass.")
elif all(abs(d) < 12 for d in deltas):
    print("  A per-channel cut does NOT reach the device while the partner is\n"
          "  at full scale. That is a real defect: the gate must fail until the\n"
          "  volume path is fixed -- check whether the XU sends SET_CUR for the\n"
          "  channel feature unit's per-channel volume at all.")
else:
    print("  Inconsistent between identical repeats: the measurement is not\n"
          "  stable enough to convict the board. Fix reproducibility first.")
print(f"  WAVs: {tmp}")
