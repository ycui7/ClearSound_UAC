#!/usr/bin/env python3
"""Case A, one dial at a time, on the FAILING artifact -- via the per-source
mixer gains I already shipped (MIXer:GAIN:HOST / :GENerator, Q15).

Why runtime and not rebuilds: the artifact ladder (factory +1.0 dB,
production +5.8, mixer-nogen +5.6, mixer+gen +11.9 over the no-stream
baseline) came from different commits, so it is suggestive, not controlled.
Flipping a gain on ONE running build is a controlled experiment: identical
binary, one variable.

Reads the mic with the acoustic rig's own analyser, so the numbers are
directly comparable to the gate's case A.

  GEN 32768 / HOST 32768  = as shipped (case A today)
  GEN 0     / HOST 32768  = generator out of the sum
  GEN 32768 / HOST 0      = host stream out of the sum
  GEN 0     / HOST 0      = both sources muted at the mixer
"""
from __future__ import annotations
import os, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ears_matrix as EM

UNITY = 32768

def scpi(cmd):
    """Quoted token, exactly as the proven rigs send it. An UNQUOTED
    REGister:READ? silently returns nothing -- that instrument bug already
    cost this investigation one false conclusion."""
    r = subprocess.run(
        ["./.venv/bin/python", "csd_diag.py", "--backend", "rawmidi",
         "--timeout", "8", cmd],
        cwd=os.path.dirname(HERE), capture_output=True, text=True, timeout=40)
    return r.stdout.strip() or r.stderr.strip()

def set_gains(w_host, w_gen):
    a = scpi(f"MIXer:GAIN:HOST {w_host}")
    b = scpi(f"MIXer:GAIN:GENerator {w_gen}")
    c = scpi("MIXer:GAIN?")
    print(f"  set host={w_host} gen={w_gen} -> board reads {c}")
    return c

def main():
    import numpy  # noqa: F401  (the analyser needs it; fail early, not mid-run)
    ears = EM.find_ears_card(None)
    dac = EM.find_dac_card(None)
    print(f"ears card {ears}, dac card {dac}")
    tmp = tempfile.mkdtemp(prefix="caseA_gains_")
    EM.ensure_capture_switch(ears)
    EM.set_volume(dac, EM.VOL_0DB, EM.VOL_0DB, tmp)
    zeros = os.path.join(tmp, "zeros.wav")
    EM.write_tone24(zeros, 3.5, amp_l=0.0, amp_r=0.0)

    arms = [("as shipped",      UNITY, UNITY),
            ("generator muted", 0,     UNITY),
            ("host muted",      UNITY, 0),
            ("both muted",      0,     0),
            ("restored",        UNITY, UNITY)]
    res = {}
    # Environmental baseline FIRST, at the same codec volume as every arm:
    # nothing streamed at all. Case A is a DELTA against this, never against
    # absolute silence -- the rig has a real ~1 kHz environment bump.
    print("\n== A0 no stream (baseline) ==")
    wav0 = os.path.join(tmp, "a0.wav")
    EM.record(dac, ears, 3, None, wav0)
    r0 = EM.analyse(wav0, tones=(EM.TONE_HZ,))
    base0 = {nm: r0[nm].tone_db[EM.TONE_HZ] for nm in "LR"}
    for nm in "LR":
        print(f"  mic{nm}: 1kHz {base0[nm]:6.1f} dBFS (no stream)")
    for name, wh, wg in arms:
        print(f"\n== {name} ==")
        out = scpi("*IDN?")
        if "ClearSound" not in out:
            print(f"  ABORT: firmware is not ours ({out!r})")
            return 1
        if "FAIL" in set_gains(wh, wg):
            print("  ABORT: gain write rejected")
            return 1
        time.sleep(0.3)
        wav = os.path.join(tmp, f"a_{wh}_{wg}.wav")
        EM.record(dac, ears, 3, zeros, wav)
        r = EM.analyse(wav, tones=(EM.TONE_HZ,))
        for nm in "LR":
            a = r[nm]
            print(f"  mic{nm}: floor {a.floor:6.1f}  1kHz {a.tone_db[EM.TONE_HZ]:6.1f} dBFS"
                  f"  SNR {a.tone_db[EM.TONE_HZ]-a.floor:+.1f}")
        res[name] = {nm: r[nm].tone_db[EM.TONE_HZ] for nm in "LR"}

    base = res["as shipped"]
    print("\n== case-A delta vs no-stream baseline, per arm ==")
    for name, _, _ in arms:
        print(f"  {name:16s} L {res[name]['L']-base0['L']:+6.1f} dB   "
              f"R {res[name]['R']-base0['R']:+6.1f} dB")
    print("\n== verdict ==")
    for name, _, _ in arms:
        if name == "as shipped":
            continue
        dl = res[name]["L"] - base["L"]
        dr = res[name]["R"] - base["R"]
        print(f"  {name:16s} L {dl:+6.1f} dB   R {dr:+6.1f} dB vs as-shipped")
    mute_d = min(base["L"] - res["both muted"]["L"],
                 base["R"] - res["both muted"]["R"])
    gen_d = base["L"] - res["generator muted"]["L"]
    if gen_d >= 6.0:
        print(f"\n  GEN-CONTRIBUTION {gen_d:+.1f} dB of the case-A artifact "
              f"-> the generator is a real source; fix it there.")
    elif mute_d < 6.0:
        print(f"\n  INCONCLUSIVE: muting BOTH mixer sources moved only "
              f"{mute_d:.1f} dB, so the case-A level is not coming from the "
              f"mixer sum at all -- nothing in the mixer path can fix it.")
    else:
        print(f"\n  mixer sum is the carrier (both-muted drops {mute_d:.1f} dB) "
              f"but neither source alone accounts for it.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
