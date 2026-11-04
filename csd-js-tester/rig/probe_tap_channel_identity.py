#!/usr/bin/env python3
"""Is the tap's own L/R identity correct? Decisive probe.

Claim under test: "captured L == captured R on 100% of frames, therefore the
device outputs mono." That claim is only sound if the tap reports the two
channels correctly in the first place. The tap holds one word per channel
(csd_mb_loop_tx[2]) written by csd_mb_loop_put(frameCount + i, ...) and read by
csd_mb_loop_take(chanIndex, ...). If the put index is not 0 and 1 -- e.g.
frameCount steps 0,2 so both half-frames address slot 0 and slot 2 is dropped
by the ch<2 guard -- the two channels cannot be distinguished, and L==R is an
instrument artifact, not a product defect.

Prior evidence that the wire is fine: the product's L/R separation is already
proven acoustically (rig/mix2_sanity.py, four legs: gen-only, host-only, both,
neither, PASS on this board). Stereo shipping as mono would have failed those
legs. So the tap is the suspect.

Method (the rule this rig follows): play a CONSTANT NONZERO word per channel
with concurrent capture, and require each captured channel to equal ITS OWN
played word, reported separately. Two constants that differ in many bits, so a
channel mix-up cannot hide behind a small difference.

  played L = 0x55AA5A, played R = 0x0F0F01  (deliberately unlike each other)

Verdicts:
  captured L == 0x55AA5A and captured R == 0x0F0F01  -> tap identity correct;
      the L==R result must come from the stimulus or the path, not the tap.
  captured L == captured R                            -> tap collapses the
      channels; the tap is wrong and no bit-perfect claim from it is valid.
  captured L == 0x0F0F01 and captured R == 0x55AA5A    -> channels swapped.
  captured L == played L and captured R == 0          -> R never written: the
      put index never reaches slot 1.
"""
import importlib.util
import os
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import csd_signal as sig                                     # noqa: E402

RATE = sig.RATE
N = 4096
WORD_L = 0x55AA5A              # 0101 0101 1010 1010 0101 1010
WORD_R = 0x0F0F01              # differs from WORD_L in 12 bits


def load_gate():
    spec = importlib.util.spec_from_file_location(
        "g", os.path.join(HERE, "csd_bitperfect_gate.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    g = load_gate()
    g.pin_path()
    print(f"\n  arm  : {g.scpi('DEBUG:LOOPback 1')}")
    print(f"  clear: {g.scpi('DEBUG:LOOPback:CLEAR')}")

    total = int(2.0 * RATE)
    sched = np.zeros((total, 2), dtype="<u4")
    sched[:, 0] = np.uint32(WORD_L) << np.uint32(8)
    sched[:, 1] = np.uint32(WORD_R) << np.uint32(8)
    addr = sched.ctypes.data

    play, pbuf = g.openp(0, g.PLAY_BUFSZ)
    cap, cbuf = g.openp(1, g.BUFSZ)
    g.alsa.snd_pcm_start(play)
    g.alsa.snd_pcm_start(cap)
    stop = threading.Event()
    st = {"written": 0}

    def writer():
        n = 0
        while not stop.is_set() and n < total:
            k = min(16384, total - n)
            off = 0
            while off < k and not stop.is_set():
                r = g.alsa.snd_pcm_writei(play, addr + (n + off) * 8, k - off)
                if r < 0:
                    if g.alsa.snd_pcm_recover(play, r, 1) < 0:
                        return
                    g.alsa.snd_pcm_start(play)
                    continue
                off += r
                n += r
                st["written"] = n

    tw = threading.Thread(target=writer, daemon=True)
    tw.start()
    time.sleep(0.5)
    buf = (g.ctypes.c_char * (N * 8))()
    got = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < 2.0:
        r = g.alsa.snd_pcm_readi(cap, buf, N)
        if r <= 0:
            g.alsa.snd_pcm_recover(cap, r if r < 0 else -1, 1)
            g.alsa.snd_pcm_start(cap)
            continue
        got.append(bytes(buf[:r * 8]))
    stop.set()
    tw.join(timeout=2)
    for p in (play, cap):
        g.alsa.snd_pcm_drop(p)
        g.alsa.snd_pcm_close(p)

    if not got:
        print("  NO CAPTURE -- probe is void")
        return
    f = np.concatenate([np.frombuffer(c, dtype="<u4") for c in got])
    L = (f[0::2] >> np.uint32(8)).astype(np.int64)
    R = (f[1::2] >> np.uint32(8)).astype(np.int64)

    print(f"  written {st['written']:,} frames, captured {len(L):,}")
    for nm, arr, want in (("L", L, WORD_L), ("R", R, WORD_R)):
        c = arr[arr != 0]
        vals, cnts = (np.unique(c, return_counts=True) if len(c)
                      else (np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)))
        top = [(hex(int(v)), int(k)) for v, k in
               sorted(zip(vals, cnts), key=lambda t: -t[1])[:3]]
        match = float((c == want).mean()) if len(c) else 0.0
        print(f"  captured {nm}: nonzero {float((arr != 0).mean()):.4f}  "
              f"== played 0x{want:06X} on {match:.4f} of nonzero")
        print(f"     top values {top}")

    nzL = float((L != 0).mean())
    mLi = float((L[L != 0] == WORD_L).mean()) if nzL else 0.0
    mRi = float((R[R != 0] == WORD_R).mean()) if float((R != 0).mean()) else 0.0
    same = float((L == R).mean())
    print(f"\n  L == R on {100.0*same:.2f}% of frames")
    print("  VERDICT")
    if mLi > 0.99 and mRi > 0.99 and same < 0.01:
        print("    TAP IDENTITY CORRECT: each captured channel equals its own "
              "played constant. The L==R result came from the stimulus or the "
              "path, NOT from the tap.")
    elif same > 0.99:
        print("    TAP COLLAPSES THE CHANNELS: both captured channels carry one "
              "value. Check csd_mb_loop_put's channel index (frameCount + i) "
              "against the loop's actual frameCount values -- if it never "
              "reaches 1, slot 1 is dead and no per-channel claim from this "
              "tap is valid.")
    elif mLi > 0.99 and float((R[R != 0] == WORD_L).mean()) > 0.99:
        print("    BOTH CHANNELS CARRY PLAYED L: host R is being dropped "
              "upstream of the tap.")
    elif mRi > 0.99 and float((L[L != 0] == WORD_R).mean()) > 0.99:
        print("    CHANNELS SWAPPED at the tap.")
    else:
        print("    Mixed result -- read the four match numbers above before "
              "attributing a cause.")


if __name__ == "__main__":
    main()
