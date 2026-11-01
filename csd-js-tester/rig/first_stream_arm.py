#!/usr/bin/env python3
"""THE decisive case-A experiment: the FIRST stream after firmware load.

case_a_gains.py settled the ordering question. Same binary, same gains
(unity/unity), measured twice:

    as shipped (first stream after load)   L +10.0  R +11.1 dB over baseline
    restored   (same gains, later in run)  L  -1.4  R  -2.1 dB over baseline

So the artifact is not a steady-state source at all -- it exists only on the
first stream after boot, and the acoustic gate always measures case A first,
which is why it has failed on every build of ours while the factory image
passes (+1.0 dB). Per-source gains are irrelevant to it: muting the generator
or the host on a LATER stream changes nothing, because a later stream is
already clean.

This rig therefore measures ONLY the first stream, with the mixer gains set
BEFORE it, so the arm under test is what the first stream actually carries.

  usage: first_stream_arm.py gen0 | genunity | host0
Run once per firmware load (run_xe.sh), so each arm is a cold board.
"""
from __future__ import annotations
import os, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ears_matrix as EM

UNITY = 32768

ARMS = {
    # name:            w_host,  w_gen
    "genunity":        (UNITY,  UNITY),   # as shipped -- the failing control
    "gen0":            (UNITY,  0),       # generator cannot inject
    "host0":           (0,      UNITY),   # host stream cannot inject
}

def scpi(cmd):
    r = subprocess.run(
        ["./.venv/bin/python", "csd_diag.py", "--backend", "rawmidi",
         "--timeout", "8", cmd],
        cwd=os.path.dirname(HERE), capture_output=True, text=True, timeout=40)
    return r.stdout.strip() or r.stderr.strip()

def main():
    arm = sys.argv[1] if len(sys.argv) > 1 else "genunity"
    if arm not in ARMS:
        print(f"unknown arm {arm!r}; want one of {sorted(ARMS)}")
        return 2
    wh, wg = ARMS[arm]

    out = scpi("*IDN?")
    if "ClearSound" not in out:
        print(f"ABORT: firmware is not ours ({out!r})")
        return 1
    print(f"LIVE BUILD: {out}")

    ears = EM.find_ears_card(None)
    dac = EM.find_dac_card(None)
    tmp = tempfile.mkdtemp(prefix="first_stream_")
    EM.ensure_capture_switch(ears)
    EM.set_volume(dac, EM.VOL_0DB, EM.VOL_0DB, tmp)

    # Gains BEFORE anything has ever streamed: the arm must be what the very
    # first frame carries, not a change applied after it.
    scpi(f"MIXer:GAIN:HOST {wh}")
    scpi(f"MIXer:GAIN:GENerator {wg}")
    read = scpi("MIXer:GAIN?")
    print(f"arm={arm} gains set host={wh} gen={wg} -> board reads {read}")
    if f"{wh},{wg}" not in read.replace(" ", ""):
        print("ABORT: gain read-back does not match the arm")
        return 1

    # Environmental baseline: capture open, DAC never streamed.
    wav0 = os.path.join(tmp, "a0.wav")
    EM.record(dac, ears, 3, None, wav0)
    r0 = EM.analyse(wav0, tones=(EM.TONE_HZ,))
    base = {nm: r0[nm].tone_db[EM.TONE_HZ] for nm in "LR"}
    print(f"A0 no stream:      L {base['L']:+6.1f}  R {base['R']:+6.1f} dBFS")

    # THE measurement: first stream this board has ever opened.
    zeros = os.path.join(tmp, "zeros.wav")
    EM.write_tone24(zeros, 3.5, amp_l=0.0, amp_r=0.0)
    wav1 = os.path.join(tmp, "first.wav")
    EM.record(dac, ears, 3, zeros, wav1)
    r1 = EM.analyse(wav1, tones=(EM.TONE_HZ,))
    d = {nm: r1[nm].tone_db[EM.TONE_HZ] for nm in "LR"}
    print(f"FIRST zeros stream: L {d['L']:+6.1f}  R {d['R']:+6.1f} dBFS")
    dl, dr = d["L"] - base["L"], d["R"] - base["R"]
    print(f"DELTA vs baseline:  L {dl:+6.1f}  R {dr:+6.1f} dB   "
          f"(gate threshold +6.0)")
    print(f"\nARM {arm}: {'FAIL' if max(dl, dr) >= 6.0 else 'PASS'}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
