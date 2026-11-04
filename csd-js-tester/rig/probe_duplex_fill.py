#!/usr/bin/env python3
"""Can this rig sustain 384 k in AND out at once? Single-threaded duplex.

WHY. Every fill deficit measured so far (0.636) has been blamed, in turn, on the
device, on the tap, on the writer's pacing, and on a mid-stream SCPI read. The
last of those was tested and refuted: moving both SCPI queries outside the
streaming window left fill at 0.6359, unchanged. What has not been tested is the
rig's own thread design. Two Python threads each making ctypes calls, against a
21 ms ring, is the remaining suspect -- and the tell was hidden in my own print:
"wrote 460,800 frames" was read AFTER join(timeout=3), so it reported the writer
eventually finishing, not delivering inside the 1.2 s capture window.

This probe replaces the two threads with the canonical ALSA full-duplex shape:
ONE thread that polls both descriptors and does the small, due transfer each
pass. No GIL contention, no pacing race between a reader and a writer, no
cross-thread event. Ring size is swept because it was already measured to matter
enormously: a 2048-frame playback ring starves the writer (288.6 k = 75 %) while
8192 sustains it (387.3 k = 101 %) -- play-only. If fill stays below 1 here too,
the deficit is not the rig's threading and the device is finally on trial.

Reported per configuration:
  in fps / out fps  -- what each direction actually achieved vs 384 000
  fill              -- fraction of captured frames that are NOT the underrun
                       substitution word (0). This is the number that has to
                       reach 1.000 before a bit-exact claim means anything:
                       every substituted zero is a frame that never compared.
"""
import ctypes
import sys
import time

sys.path.insert(0, "rig")

WORD_L = 0x55AA5A
WORD_R = 0x0F0F01


def scpi(cmd):
    import subprocess
    r = subprocess.run(
        [".venv/bin/python", "csd_diag.py", "--backend", "rawmidi",
         "--timeout", "15", cmd],
        capture_output=True, text=True, timeout=40)
    return (r.stdout or "").strip().replace("\r", "")


def main():
    import numpy as np
    import csd_signal as sig
    R = sig.RATE

    a = ctypes.CDLL("libasound.so.2")
    for fn, rt, at in (
            ("snd_pcm_writei", ctypes.c_long,
             [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
            ("snd_pcm_readi", ctypes.c_long,
             [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
            ("snd_pcm_avail_update", ctypes.c_long, [ctypes.c_void_p])):
        getattr(a, fn).restype = rt
        getattr(a, fn).argtypes = at

    def op(st, bufsz):
        p = ctypes.c_void_p()
        assert a.snd_pcm_open(ctypes.byref(p), b"hw:3,0", st, 0) >= 0
        hp = ctypes.c_void_p()
        a.snd_pcm_hw_params_malloc(ctypes.byref(hp))
        a.snd_pcm_hw_params_any(p, hp)
        a.snd_pcm_hw_params_set_access(p, hp, 3)
        a.snd_pcm_hw_params_set_format(p, hp, 10)             # S32_LE
        a.snd_pcm_hw_params_set_channels(p, hp, 2)
        d = ctypes.c_uint(R)
        a.snd_pcm_hw_params_set_rate_near(p, hp, ctypes.byref(d), None)
        b = ctypes.c_uint(bufsz)
        a.snd_pcm_hw_params_set_buffer_size_near(p, hp, ctypes.byref(b))
        per = ctypes.c_uint(min(bufsz // 4, 4096))
        a.snd_pcm_hw_params_set_period_size_near(p, hp, ctypes.byref(per), None)
        assert a.snd_pcm_hw_params(p, hp) >= 0, f"hw_params bufsz={bufsz}"
        a.snd_pcm_prepare(p)
        return p

    print("IDN:", scpi("*IDN?"))
    scpi("DIAG:DSP:PRESet 2")
    scpi("DEBUG:LOOPback 1")

    SECS = 1.5
    n = int(R * SECS)
    sched = np.zeros((n, 2), dtype="<u4")
    sched[:, 0] = np.uint32(WORD_L) << np.uint32(8)
    sched[:, 1] = np.uint32(WORD_R) << np.uint32(8)
    base = sched.ctypes.data

    for bufsz in (8192, 32768, 131072):
        play = op(0, bufsz)
        cap = op(1, bufsz)
        a.snd_pcm_start(play)
        a.snd_pcm_start(cap)

        rbuf = (ctypes.c_char * (4096 * 8))()
        got = []
        woff = 0
        errs = 0
        t0 = time.monotonic()
        # ONE thread, both directions. Each pass moves what is due and returns;
        # neither direction can be starved by a thread that is blocked on the
        # other, which is the failure mode the two-thread version had.
        while time.monotonic() - t0 < SECS:
            did = False
            av = a.snd_pcm_avail_update(play)
            if av >= 512 and woff < n:
                k = min(av, 4096, n - woff)
                r = a.snd_pcm_writei(play, base + woff * 8, k)
                if r < 0:
                    if a.snd_pcm_recover(play, r, 1) < 0:
                        errs += 1
                        break
                else:
                    woff += r
                    did = True
            av = a.snd_pcm_avail_update(cap)
            if av >= 512:
                k = min(av, 4096)
                r = a.snd_pcm_readi(cap, rbuf, k)
                if r <= 0:
                    if a.snd_pcm_recover(cap, r if r < 0 else -1, 1) < 0:
                        errs += 1
                        break
                else:
                    got.append(bytes(rbuf[:r * 8]))
                    did = True
            if not did:
                time.sleep(0.0002)
        el = time.monotonic() - t0

        a.snd_pcm_close(play)
        a.snd_pcm_close(cap)
        # Witness after the streams close -- a rawmidi query mid-stream costs
        # tens of ms and the ring is only ~21 ms deep.
        witness = scpi("DEBUG:LOOPback?")

        if not got:
            print(f"buf={bufsz:>7}  NO CAPTURE (errs={errs}) -- void")
            continue
        f = np.concatenate([np.frombuffer(c, dtype="<u4") for c in got])
        L = (f[0::2] >> np.uint32(8)).astype(np.int64)
        Rr = (f[1::2] >> np.uint32(8)).astype(np.int64)
        nz = (L != 0) & (Rr != 0)
        nnz = int(nz.sum())
        fill = float(nz.mean())
        same_nz = float((L[nz] == Rr[nz]).mean()) if nnz else float("nan")
        print(f"buf={bufsz:>7}  out {woff/el:>10,.0f} f/s   "
              f"in {len(L)/el:>10,.0f} f/s   fill={fill:.4f}  "
              f"L==R(nonzero)={same_nz:.4f}  errs={errs}  tap={witness}")
        print(f"          target {R:,} f/s -> out {100*woff/el/R:.2f}% "
              f"in {100*len(L)/el/R:.2f}%")

    scpi("DEBUG:LOOPback 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
