#!/usr/bin/env python3
"""Ask the device where its tap's put()/take() calls actually land.

WHY THIS EXISTS. The tap stores tx[put_index] on TX and returns tx[take_index]
on RX. Both indices are computed in source and both LOOK right (TX uses
frameCount+i at its port write; RX uses the matching half-frame expression),
yet on hardware both captured channels return host R's word and host L never
appears. A captured word carries no record of the slot it came from, so no
amount of capture-side analysis can separate the three candidate causes:

  1. TX stores both channels into the SAME slot (one site dead, or both
     channels arriving with the same index);
  2. RX asks for the WRONG slot;
  3. the half-frame that injects L is not compiled at all -- plausible because
     AUD_TO_USB_RATIO==1, and one of the two RX injections sits inside
     #if (AUD_TO_USB_RATIO > 1).

DEBUG:LOOPback:INDEX? returns the device's own counters: put arrivals per
channel, take arrivals per channel, and per-channel arrivals at each of the two
TX call sites. That is the observation the capture stream cannot provide.

Reading, not guessing:
  site0[]/site1[]   -- if one row is all zeros, that half-frame does not run
                       and the channel it owns keeps the PREVIOUS frame's word:
                       exactly the observed collapse.
  put_ch[0..3]      -- which indices TX actually presents. Only [0] nonzero
                       means both channels were keyed to slot 0.
  take_ch[0..3]     -- which indices RX actually asks for.

Build stamp (last field) must be 2 for this instrumented tap; anything else
means the rig measured a stale image.
"""
import subprocess
import sys
import time

sys.path.insert(0, "rig")

EXPECT_BUILD = 2


def scpi(cmd):
    r = subprocess.run(
        [".venv/bin/python", "csd_diag.py", "--backend", "rawmidi",
         "--timeout", "15", cmd],
        capture_output=True, text=True, timeout=40)
    out = (r.stdout or "").strip().replace("\r", "")
    if not out:
        print(f"  SCPI EMPTY for {cmd!r}: {(r.stderr or '').strip()[:160]}")
    return out


def main():
    print("IDN:", scpi("*IDN?"))
    print("arm :", scpi("DEBUG:LOOPback 1"))
    print("clear:", scpi("DEBUG:LOOPback:CLEAR"))

    # Stream real audio: the counters only move if the I2S loop is running and
    # the tap is reached. A quiet device would report all zeros for the wrong
    # reason, so the run must carry signal.
    import ctypes
    import threading
    import numpy as np
    import csd_signal as sig

    R = sig.RATE
    a = ctypes.CDLL("libasound.so.2")
    for f, rt, at in (("snd_pcm_writei", ctypes.c_long,
                       [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
                      ("snd_pcm_avail_update", ctypes.c_long,
                       [ctypes.c_void_p])):
        getattr(a, f).restype = rt
        getattr(a, f).argtypes = at

    def openp():
        p = ctypes.c_void_p()
        assert a.snd_pcm_open(ctypes.byref(p), b"hw:3,0", 0, 0) >= 0
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

    play = openp()
    # Distinct per-channel content so a cross-channel collapse is visible even
    # in the counters' neighbours.
    n = 200_000
    sched = np.empty(n * 2, dtype="<u4")
    sched[0::2] = (sig.noise24_vec(n, ch=0) << np.uint32(8)) | 0x8000_0000
    sched[1::2] = (sig.noise24_vec(n, ch=1) << np.uint32(8)) | 0x8000_0000
    base = sched.ctypes.data
    a.snd_pcm_start(play)

    st = {"w": 0, "fatal": None}
    stop = threading.Event()

    def writer():
        off = 0
        while off < n and not stop.is_set():
            r = a.snd_pcm_writei(play, base + off * 8, n - off)
            if r < 0:
                rc = a.snd_pcm_recover(play, r, 1)
                if rc < 0:
                    st["fatal"] = f"recover {rc}"
                    return
                a.snd_pcm_start(play)
                continue
            off += r
            st["w"] = off

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    time.sleep(1.2)                     # measure a steady-state second
    print("\n--- counters after ~1.2 s of live 384k audio ---")
    print("frames written by host:", st["w"], "fatal:", st["fatal"])
    stop.set()

    for label, cmd in (("LOOPback?      ", "DEBUG:LOOPback?"),
                       ("LOOPback:INDEX?", "DEBUG:LOOPback:INDEX?")):
        raw = scpi(cmd)
        print(f"\n{label} -> {raw}")
        if cmd.endswith("INDEX?"):
            try:
                v = [int(x) for x in raw.split(",")]
            except ValueError:
                print("  PARSE FAILED")
                return 2
            if len(v) != 17:
                print(f"  EXPECTED 17 fields, got {len(v)}")
                return 2
            names = ("put_ch0", "put_ch1", "put_ch2", "put_ch3",
                     "take_ch0", "take_ch1", "take_ch2", "take_ch3",
                     "site0_c0", "site0_c1", "site0_c2", "site0_c3",
                     "site1_c0", "site1_c1", "site1_c2", "site1_c3")
            for nm, val in zip(names, v[:16]):
                print(f"    {nm:<9} {val:,}")
            build = v[16]
            print(f"    tap build {build}  (expect {EXPECT_BUILD})"
                  + ("" if build == EXPECT_BUILD else "   <-- STALE IMAGE"))
            if build != EXPECT_BUILD:
                return 2

            # Verdicts the counters were built to give.
            print("\n--- verdict ---")
            s0 = sum(v[8:12])
            s1 = sum(v[12:16])
            print(f"  site0 total {s0:,}   site1 total {s1:,}")
            if s0 == 0 or s1 == 0:
                dead = "site0 (first half-frame)" if s0 == 0 else "site1 (second half-frame)"
                print(f"  DEAD SITE: {dead} never ran -> the channel it owns"
                      f" keeps the PREVIOUS frame's word. That reproduces the"
                      f" observed L==R collapse.")
            p0, p1 = v[0], v[1]
            print(f"  put_ch[0]={p0:,}  put_ch[1]={p1:,}")
            if p0 and not p1:
                print("  BOTH channels were keyed to slot 0 -> TX collapse.")
            t0, t1 = v[4], v[5]
            print(f"  take_ch[0]={t0:,}  take_ch[1]={t1:,}")
            if t0 and not t1:
                print("  RX only ever asked for slot 0 -> both capture channels"
                      " read the same slot: RX-side collapse.")
            if t0 == 0 and t1 == 0:
                print("  RX take() never called: the tap's read injection is"
                      " NOT COMPILED (check AUD_TO_USB_RATIO guard).")
            hi = [v[i] for i in (2, 3, 6, 7)]
            if any(hi):
                print(f"  out-of-range indices seen: {hi} -- the tap's channel"
                      " expression exceeds NUM_USB_CHAN_*")
    a.snd_pcm_close(play)
    return 0


if __name__ == "__main__":
    sys.exit(main())
