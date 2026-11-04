#!/usr/bin/env python3
"""Armed vs disarmed: does the tap's injection reach the host AT ALL?

The decisive discriminator, and it needs no new firmware. If the capture stream
is IDENTICAL whether the tap is armed or disarmed, the injected words never
reach the host and every capture-side conclusion ever drawn from this tap --
including the "L == R channel collapse" -- measured something else entirely.

Why this is the right question now: with DSP preset 2 the capture's L and R came
back DISTINCT and both approximately equal to the words the host PLAYED, while
the tap reported serving completely different (post-DSP) words. Those two facts
cannot both be true of one data path. The candidates:

  - the host capture slots are filled from the mixer's DAC-bound slots rather
    than the ADC slots (a samples_to_host_map offset error would show the host
    its own played audio, L/R preserved -- exactly what was seen);
  - the injection writes a buffer that the USB capture never reads.

Armed-vs-disarmed separates "tap is not delivering" from "map is wrong" without
touching either: a map that reads the mixer path is unaffected by arm state, so
byte-identical captures across the two states is the map/theatre signature.

Same image, same stimulus, same preset in both legs -- the ONLY difference is
DEBUG:LOOPback 1 vs 0. The tap's own counters are printed for both legs so a
leg whose tap never ran is visible as such, not silently compared.
"""
import ctypes
import sys
import threading
import time

sys.path.insert(0, "rig")

WORD_L = 0x55AA5A
WORD_R = 0x0F0F01
SECS = 1.2
PRESET = 2          # pin it: the tap reads post-DSP words, so preset is stimulus


def scpi(cmd):
    import subprocess
    r = subprocess.run(
        [".venv/bin/python", "csd_diag.py", "--backend", "rawmidi",
         "--timeout", "15", cmd],
        capture_output=True, text=True, timeout=40)
    return (r.stdout or "").strip().replace("\r", "")


def open_stream(a, st, R):
    p = ctypes.c_void_p()
    assert a.snd_pcm_open(ctypes.byref(p), b"hw:3,0", st, 0) >= 0
    hp = ctypes.c_void_p()
    a.snd_pcm_hw_params_malloc(ctypes.byref(hp))
    a.snd_pcm_hw_params_any(p, hp)
    a.snd_pcm_hw_params_set_access(p, hp, 3)
    a.snd_pcm_hw_params_set_format(p, hp, 10)          # S32_LE
    a.snd_pcm_hw_params_set_channels(p, hp, 2)
    d = ctypes.c_uint(R)
    a.snd_pcm_hw_params_set_rate_near(p, hp, ctypes.byref(d), None)
    b = ctypes.c_uint(8192)
    a.snd_pcm_hw_params_set_buffer_size_near(p, hp, ctypes.byref(b))
    assert a.snd_pcm_hw_params(p, hp) >= 0
    a.snd_pcm_prepare(p)
    return p


def run_leg(a, np, sig, armed, R, n, sched):
    """One leg. Returns (stats dict, capture array) or (None, None)."""
    base = sched.ctypes.data
    print(f"\n=== leg: tap {'ARMED' if armed else 'DISARMED'} ===")
    scpi(f"DEBUG:LOOPback {1 if armed else 0}")
    st = {"w": 0, "fatal": None}
    stop = threading.Event()
    chunks = []

    play = open_stream(a, 0, R)
    cap = open_stream(a, 1, R)

    def writer():
        off = 0
        while off < n and not stop.is_set():
            r = a.snd_pcm_writei(play, base + off * 8, n - off)
            if r < 0:
                if a.snd_pcm_recover(play, r, 1) < 0:
                    st["fatal"] = "write recover"
                    return
                a.snd_pcm_start(play)
                continue
            off += r
            st["w"] = off

    def reader():
        buf = (ctypes.c_char * (4096 * 8))()
        while not stop.is_set():
            r = a.snd_pcm_readi(cap, buf, 4096)
            if r <= 0:
                if a.snd_pcm_recover(cap, r if r < 0 else -1, 1) < 0:
                    return
                a.snd_pcm_start(cap)
                continue
            chunks.append(bytes(buf[:r * 8]))

    a.snd_pcm_start(play)
    a.snd_pcm_start(cap)
    tw = threading.Thread(target=writer, daemon=True)
    tr = threading.Thread(target=reader, daemon=True)
    tw.start(); tr.start()
    time.sleep(SECS)
    stop.set(); tw.join(timeout=3); tr.join(timeout=3)
    a.snd_pcm_close(play); a.snd_pcm_close(cap)
    # Witness read AFTER the streams close. A rawmidi SCPI round-trip costs tens
    # of milliseconds; the capture ring is 8192 frames = 21 ms, so querying
    # mid-stream starved the reader and fabricated 36% zero frames -- a rig fault
    # that was being read as device underfill. The tap's counters persist, so
    # asking late costs nothing.
    witness = scpi("DEBUG:LOOPback?")
    words = scpi("DEBUG:LOOPback:INDEX?")

    if not chunks:
        print("  NO CAPTURE -- leg void, not a negative")
        return None, None
    f = np.concatenate([np.frombuffer(c, dtype="<u4") for c in chunks])
    print(f"  wrote {st['w']:,} frames  captured {len(f)//2:,}  fatal={st['fatal']}")
    print(f"  tap witness (en,frames): {witness}")
    try:
        v = [int(x) for x in words.split(",")]
        print(f"  tap site words: site0=0x{v[17]:08X} site1=0x{v[18]:08X}"
              f"  tx0=0x{v[19]:08X} tx1=0x{v[20]:08X}  build={v[16]}")
    except (ValueError, IndexError):
        print(f"  INDEX? unparsable: {words!r}")
    return st, f


def describe(np, f, tag):
    L = (f[0::2] >> np.uint32(8)).astype(np.int64)
    R = (f[1::2] >> np.uint32(8)).astype(np.int64)
    # Report L==R ONLY among frames where BOTH channels are nonzero. Zero frames
    # are the USB-underrun signature (lib_xua substitutes underflowWord = 0 on
    # both channels), and 0 == 0, so an all-frames equality count reports the
    # underfill ratio as if it were a channel collapse. That conflation is what
    # made an earlier run look like "L == R on 100% of frames".
    nz = (L != 0) & (R != 0)
    nnz = int(nz.sum())
    same_all = float((L == R).mean())
    same_nz = float((L[nz] == R[nz]).mean()) if nnz else float("nan")
    fill = float(((L != 0) | (R != 0)).mean())
    print(f"  {tag}: frames={len(L):,} fill={fill:.4f} nonzero-pairs={nnz:,}"
          f"  L==R(all)={same_all:.4f}  L==R(nonzero only)={same_nz:.4f}")
    if nnz:
        print(f"          median L(nonzero)=0x{int(np.median(L[nz])):06X}"
              f"  median R(nonzero)=0x{int(np.median(R[nz])):06X}")
    return L, R, nnz, same_nz, fill


def main():
    import numpy as np
    import csd_signal as sig
    R = sig.RATE
    print("IDN:", scpi("*IDN?"))
    scpi(f"DIAG:DSP:PRESet {PRESET}")
    print(f"DIAG:DSP:PRESet? : {scpi('DIAG:DSP:PRESet?')}  (asked {PRESET})")

    n = int(R * SECS)
    sched = np.zeros((n, 2), dtype="<u4")
    sched[:, 0] = np.uint32(WORD_L) << np.uint32(8)
    sched[:, 1] = np.uint32(WORD_R) << np.uint32(8)

    a = ctypes.CDLL("libasound.so.2")
    for fn, rt, at in (("snd_pcm_writei", ctypes.c_long,
                        [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
                       ("snd_pcm_readi", ctypes.c_long,
                        [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong])):
        getattr(a, fn).restype = rt
        getattr(a, fn).argtypes = at

    st_a, fa = run_leg(a, np, sig, True, R, n, sched)
    st_d, fd = run_leg(a, np, sig, False, R, n, sched)
    if fa is None or fd is None:
        print("\nABORT: a leg captured nothing -- comparison void.")
        return 2

    print("\n--- the two captures ---")
    La, Ra, na, same_a, fill_a = describe(np, fa, "ARMED   ")
    Ld, Rd, nd, same_d, fill_d = describe(np, fd, "DISARMED")

    m = min(len(La), len(Ld))
    ident = float((La[:m] == Ld[:m]).mean())
    print(f"\n  identical L across legs on {ident:.4f} of {m:,} frames")
    print("\n--- verdict ---")
    if ident > 0.999:
        print("  THE TAP CHANGES NOTHING on the host capture stream. The injected\n"
              "  words never reach USB, so every capture-side claim from this tap\n"
              "  (including the channel collapse) is VOID -- it measured the normal\n"
              "  ADC path. Fix the delivery path (audiohub samplesIn -> mixer\n"
              "  GetSamplesFromDevice -> samples_to_host_map), not the tap.")
    elif ident < 0.05:
        print("  Tap delivery CONFIRMED: the two legs are unrelated streams. The\n"
              "  capture really is the tap's words; judge the channel claim from the\n"
              "  L==R and median figures above.")
    else:
        print(f"  PARTIAL ({ident:.4f}): some frames carry injected words and some\n"
              "  do not. Do not summarise -- the arm state is being ignored per-frame\n"
              "  somewhere, which is its own bug. Get the per-frame pattern.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
