#!/usr/bin/env python3
"""Does an asymmetric volume request lose its ANALOG term to the other channel?

Case F (ask 64,192 = -32 dB on the driven channel) measures only -7.9 dB. It is
reproducible to 0.05 dB, so it is not a flake. Codec registers explain half of
it and implicate the rest:

  0x0F=16  -> digital code 16 at 0.5 dB/LSB = -8.0 dB. Correct, as computed.
  0x10=0   -> the other channel at 0 dB. Correct.
  0x03=64  -> analog field 0 = 0 dB. WRONG: it should hold -24 dB.

The firmware's split, ADVolume_Split(), divides a host request between an
analog stage and a digital trim, giving analog priority up to A_RANGE = -24 dB:

  -32 dB request -> AVol = -24 dB, DVol = -8 dB
  digital code   = (8*256 + 64)/128 = 16            <- matches 0x0F exactly
  analog field   = (24*256) >> 8     = 24            <- never arrived

So the digital path is fine and the analog term is what goes missing. The
mechanism is visible in the code: register 0x03 (ES9219Q_ANALOG_VOL_CTRL) is
ONE register, written from a per-channel value, and its dedupe state
(volRegLastA / volRegValidA) is also a single pair shared by both channels.
When a host request asks for different levels on the two channels, each channel
sends its own update, both write the same register, and the last one wins.
Channel 1 at full scale therefore writes 0 dB over channel 0's -24 dB.

That hypothesis makes a sharp prediction which separates it from every rival
explanation. A SYMMETRIC request cannot lose the fight, because both channels
ask for the same analog value:

  192,192  analog 0    digital 0     -> reference
  64,192   analog -24  digital -8    -> predicted -8   (asymmetric: clobbered)
  192,64   analog -24  digital -8    -> predicted -8   (asymmetric: clobbered)
  64,64    analog -24  digital -8    -> predicted -32  (symmetric: both agree)

If 64,64 reaches -32 while the asymmetric pairs stop at -8, the shared analog
register and its shared dedupe are the defect, and the fix is per-channel
analog dedupe plus a decision about what to do when the two channels want
different analog levels -- one register cannot serve both, so the split must
either keep analog common (use the LOUDER of the two, or hold it at 0 dB and
attenuate entirely in the digital domain, which is 0..-127.5 dB at 0.5 dB/LSB
and has no reason not to carry the whole request).

If all four come out -8, the analog path is dead for this build entirely.
If all four come out right, the earlier -8 dB was a harness condition.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ears_matrix as m  # noqa: E402

DAC = m.find_dac_card(None)
EARSC = m.find_ears_card(None)
REG_LO, REG_HI, REG_ANA = 0x0F, 0x10, 0x03
CSDDIAG = os.path.dirname(os.path.abspath(__file__ + "/.."))
PY = os.path.join(CSDDIAG, ".venv", "bin", "python")
print(f"DAC {DAC}  probe {EARSC}")


def read_regs(addrs):
    import base64
    payload = base64.b64encode(bytes(addrs)).decode()
    r = subprocess.run(
        [PY, os.path.join(CSDDIAG, "csd_diag.py"), "--backend", "rawmidi",
         "--timeout", "8", f'REGister:READ? "{payload}"'],
        capture_output=True, text=True, cwd=CSDDIAG, timeout=40)
    for ln in reversed([l.strip() for l in r.stdout.splitlines() if l.strip()]):
        try:
            v = base64.b64decode(ln + "=" * (-len(ln) % 4))
        except Exception:
            continue
        if len(v) == len(addrs):
            return list(v)
    return None


def one(tag, amp_on, vl, vr, tmp):
    """Play the tone on ONE side and read back registers + level."""
    al, ar = (amp_on, 0.0) if tag.endswith("L") else (0.0, amp_on)
    stim = os.path.join(tmp, f"{tag}.stim.wav")
    cap = os.path.join(tmp, f"{tag}.cap.wav")
    m.set_volume(DAC, vl, vr, tmp)
    regs = read_regs([REG_LO, REG_HI, REG_ANA])
    m.write_tone24(stim, 3.5, al, ar)
    m.record(DAC, EARSC, 3, stim, cap)
    res = m.analyse(cap, tones=(m.TONE_HZ,))
    lv, rv = res["L"].tone_db[m.TONE_HZ], res["R"].tone_db[m.TONE_HZ]
    driven = max(lv, rv)
    if regs:
        ana_field = regs[2] & 0x1F
        rs = (f"0x0F={regs[0]:3d}(-{regs[0] * 0.5:5.1f}) "
              f"0x10={regs[1]:3d}(-{regs[1] * 0.5:5.1f}) "
              f"0x03={regs[2]:3d} ana_field={ana_field:2d}(-{ana_field:2d})")
    else:
        rs = "REGISTER READ FAILED"
    print(f"  {tag:8s} ask {vl:3d},{vr:3d}  {rs}  driven {driven:+6.1f} dBFS")
    return driven, regs


tmp = tempfile.mkdtemp(prefix="vol_ana_")
print("\n  (digital codes are shown as the attenuation they command,")
print("   0.5 dB/LSB; ana_field is register 0x03's 5-bit analog field)")

ref, _ = one("ref_L", 0.8, m.VOL_0DB, m.VOL_0DB, tmp)
a1, r1 = one("asym_L", 0.8, m.VOL_M32, m.VOL_0DB, tmp)
a2, r2 = one("asym_R", 0.8, m.VOL_0DB, m.VOL_M32, tmp)
sym, rs_ = one("sym_L", 0.8, m.VOL_M32, m.VOL_M32, tmp)
refR, _ = one("ref_R", 0.8, m.VOL_0DB, m.VOL_0DB, tmp)

# The gate is one question, asked three ways: does the driven cavity land at
# the attenuation the host actually requested? Tolerance 4 dB, which is wider
# than this rig's 0.05 dB repeatability and than the codec's 0.5 dB digital
# step, and far tighter than the 24 dB the defect costs.
ROWS = (("asymmetric L (64,192)", a1, ref),
        ("asymmetric R (192,64)", a2, refR),
        ("symmetric    (64,64) ", sym, ref))
print(f"\n  every row must land at -32 dB relative to its own side's reference:")
bad = []
for label, got, base in ROWS:
    d = got - base
    ok = abs(d + 32) <= 4
    print(f"    {label}: {d:+6.1f} dB  {'OK' if ok else 'FAIL (need -32 +/- 4)'}")
    if not ok:
        bad.append(label)

if rs_ is None or r1 is None or r2 is None:
    print("\n  Register path failed -- report the numbers, no verdict.")
    print("ANALOG_VOLUME_GATE: INCONCLUSIVE")
    sys.exit(2)

if bad:
    print(f"\n  {len(bad)} of 3 rows do not honor the request: "
          + ", ".join(b.strip() for b in bad))
    if abs(a1 - ref + 8) <= 4 and abs(a2 - refR + 32) <= 4:
        print("  Pattern: the asymmetric case whose SILENT channel is L stops at")
        print("  the digital trim alone. Register 0x03 is one shared analog")
        print("  register driven from a per-channel split (datasheet reg 3:")
        print("  ANALOG_VOLUME_MAIN controls both channels), so the louder")
        print("  channel overwrites the quieter channel's analog term.")
    print("ANALOG_VOLUME_GATE: FAIL")
else:
    print("\n  All three honor the request: the shared analog register now")
    print("  carries only what both channels ask for, and the per-channel")
    print("  difference is made up in the digital volume as it must be.")
    print("ANALOG_VOLUME_GATE: PASS")
print(f"  WAVs: {tmp}")
sys.exit(0 if not bad else 1)
