#!/usr/bin/env python3
"""One run, six numbers, one verdict: where does host L disappear?

WHY A SIXTH VIEW WAS NEEDED. Three earlier probes all concluded "both captured
channels carry host R's word", and all three read only the capture stream. That
stream cannot separate the candidates, because a captured word carries no record
of which samplesOut slot it came from. Adding the tap's index counters (build 2)
showed the tap is innocent -- site0 writes slot 0 only, site1 writes slot 1
only, RX asks for both, no out-of-range indices -- but counters prove INDEX, and
the fault is a VALUE fault. Source review then cleared every device writer:
mixer fills ptr_samples[i], the device send reads ptr_samples[deviceMap[i]]
with an identity map (mixer.xc:1233), and the audiohub fills samplesOut[i] =
inuint(c_aud) per channel. Every one is indexed by i. So the remaining
candidates need the words themselves.

Six numbers from ONE run:
  played   L, R          host side, what we put on the wire
  device   site_word[0], site_word[1]   what each TX site saw in samplesOut
  device   tx[0], tx[1]                 what the tap serves the two RX slots
  captured L, R          what came back over USB

Verdict table (PL = played L, PR = played R):
  site_word == (PL, PR) and tx == (PL, PR) and capture == (PR, PR)
      -> tap and pipeline are both fine; the fault is in the RX injection or in
         THIS RIG'S capture parsing. Suspect the rig first.
  site_word == (PL, PR) and tx == (PR, PR)
      -> fault in the tap's read side (csd_mb_loop_take / its callers).
  site_word == (PR, PR)
      -> host L never reaches samplesOut[0]. The fault is upstream: decouple ->
         mixer -> audiohub. Product bug.
  site_word == (PL, 0)
      -> site1 never ran; slot 1 holds a stale word.

Stimulus is a constant per-channel word so a torn device-side read cannot
change the answer, and L/R are 12 bits apart so no plausible aliasing turns one
into the other.
"""
import ctypes
import sys
import threading
import time

sys.path.insert(0, "rig")

EXPECT_BUILD = 4
WORD_L = 0x55AA5A           # 24-bit; goes on the wire shifted << 8
WORD_R = 0x0F0F01
SECS = 1.5
# Preset under test. Passed in via --dsp so ONE binary can be measured in both
# states. This is the only variable that separated the two earlier observations:
# with preset 0 both capture channels returned played R (collapse); with the
# build default 2 they came back distinct. csd_dsp_frame():187 returns early on
# preset 0 ("byte-exact bypass"), so the preset decides whether the outgoing
# buffer is rewritten at all -- which makes it the variable worth pinning.
# A collapse that appears ONLY on the bypass path is a product bug: bypass would
# be mono, and the acoustic gate that proved L/R separation ran on the FIR path.


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

    # --dsp N pins the engine preset before streaming. Needed because the tap
    # reads samplesOut AFTER UserBufferManagement ran csd_dsp_frame in place:
    # the captured words are post-DSP, so the preset is part of the stimulus
    # chain and must be identical across comparisons, like every other lever.
    preset = 2
    if "--dsp" in sys.argv:
        preset = int(sys.argv[sys.argv.index("--dsp") + 1])

    R = sig.RATE
    print("IDN:", scpi("*IDN?"))
    scpi(f"DIAG:DSP:PRESet {preset}")
    got = scpi("DIAG:DSP:PRESet?")
    print(f"DIAG:DSP:PRESet? : {got}   (asked for {preset})")
    if got.split(",")[0].strip() != str(preset):
        print("ABORT: preset did not adopt -- the run would measure an unknown"
              " DSP state, which is how the last two runs disagreed.")
        return 2
    print(f"played L = 0x{WORD_L:06X}   played R = 0x{WORD_R:06X}"
          f"   (wire words 0x{WORD_L << 8:08X} / 0x{WORD_R << 8:08X})")

    n = int(R * SECS)
    # (frames, channels) so sched[:,0]/sched[:,1] below mean what they say.
    # C-contiguous row-major, so the flat wire order is L0 R0 L1 R1 ... which is
    # what ALSA's S32_LE interleaved format wants -- verified by the capture
    # side reading the same stride.
    sched = np.zeros((n, 2), dtype="<u4")
    sched[:, 0] = np.uint32(WORD_L) << np.uint32(8)
    sched[:, 1] = np.uint32(WORD_R) << np.uint32(8)
    base = sched.ctypes.data

    a = ctypes.CDLL("libasound.so.2")
    for f, rt, at in (("snd_pcm_writei", ctypes.c_long,
                       [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
                      ("snd_pcm_readi", ctypes.c_long,
                       [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
                      ("snd_pcm_avail_update", ctypes.c_long,
                       [ctypes.c_void_p])):
        getattr(a, f).restype = rt
        getattr(a, f).argtypes = at

    def op(st):
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

    play, cap = op(0), op(1)
    print("arm :", scpi("DEBUG:LOOPback 1"))

    st = {"w": 0, "fatal": None}
    stop = threading.Event()

    def writer():
        off = 0
        while off < n and not stop.is_set():
            r = a.snd_pcm_writei(play, base + off * 8, n - off)
            if r < 0:
                if a.snd_pcm_recover(play, r, 1) < 0:
                    st["fatal"] = "write recover failed"
                    return
                a.snd_pcm_start(play)
                continue
            off += r
            st["w"] = off

    chunks = []

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
    time.sleep(SECS)                      # sample mid-stream, before the writer
    raw = scpi("DEBUG:LOOPback:INDEX?")   # drains the endpoint under load
    stop.set(); tw.join(timeout=3); tr.join(timeout=3)

    print(f"\nhost wrote {st['w']:,} frames, captured {sum(len(c) for c in chunks)//8:,}"
          f" fatal={st['fatal']}")

    try:
        v = [int(x) for x in raw.split(",")]
    except ValueError:
        print(f"INDEX? PARSE FAILED: {raw!r}")
        return 2
    if len(v) != 21:
        print(f"EXPECTED 21 fields, got {len(v)}: {raw}")
        return 2
    build = v[16]
    print(f"\ntap build {build} (expect {EXPECT_BUILD})"
          + ("" if build == EXPECT_BUILD else "   <-- STALE IMAGE, verdict void"))
    if build != EXPECT_BUILD:
        return 2

    sw0, sw1, tx0, tx1 = v[17], v[18], v[19], v[20]
    EW_L, EW_R = WORD_L << 8, WORD_R << 8

    def fmt(x):
        return f"0x{x:08X}" if x else "0"

    # An assertion, not a print: csd_mb_loop_set_enable zeroes site_word/tx, so a
    # zero here means that site has not written since arming -- the tap is not
    # running, and every device-side number in this report is meaningless. The
    # previous version of this probe printed such numbers without checking, which
    # is how a stale pair from an earlier leg got read as a measurement.
    if sw0 == 0 or sw1 == 0 or tx0 == 0 or tx1 == 0:
        print(f"\nABORT: tap words are zero (site=({sw0},{sw1}) tx=({tx0},{tx1}))"
              "\n  -> that TX site never wrote since arming. Nothing below this"
              "\n  line can be believed, so it is not printed.")
        return 2

    print("\n--- the six numbers ---")
    print(f"  played      L {fmt(EW_L):>12}   R {fmt(EW_R):>12}")
    print(f"  device site0 (samplesOut seen at TX site 0)   {fmt(sw0)}")
    print(f"  device site1 (samplesOut seen at TX site 1)   {fmt(sw1)}")
    print(f"  device tx[0] (served to RX slot 0)            {fmt(tx0)}")
    print(f"  device tx[1] (served to RX slot 1)            {fmt(tx1)}")

    if chunks:
        f = np.concatenate([np.frombuffer(c, dtype="<u4") for c in chunks])
        capL = (f[0::2] >> np.uint32(8)).astype(np.int64)
        capR = (f[1::2] >> np.uint32(8)).astype(np.int64)
        nzL = int((capL != 0).sum())
        nzR = int((capR != 0).sum())
        mL = float((capL == WORD_L).sum()) / nzL if nzL else 0.0
        mR = float((capR == WORD_R).sum()) / nzR if nzR else 0.0
        print(f"  captured    L {fmt(int(np.median(capL)))}"
              f"   (match played L on {mL:.4f} of {nzL:,} nonzero)")
        print(f"  captured    R {fmt(int(np.median(capR)))}"
              f"   (match played R on {mR:.4f} of {nzR:,} nonzero)")
    else:
        mL = mR = 0.0
        print("  captured    NOTHING READING")

    def isv(x, w):
        return x == w

    print("\n--- verdict ---")
    if isv(sw0, EW_L) and isv(sw1, EW_R):
        if isv(tx0, EW_L) and isv(tx1, EW_R):
            print("  DEVICE IS CLEAN END TO END: samplesOut holds L at slot 0 and"
                  "\n  R at slot 1, and the tap serves them distinctly. A capture"
                  "\n  that still shows L==R is therefore THIS RIG's capture-side"
                  "\n  parsing or the RX injection -- check the rig before the box.")
        else:
            print("  TAP READ SIDE FAULTY: sites saw L and R correctly but tx[] is"
                  "\n  not (L,R). Look at csd_mb_loop_take and its call sites.")
    elif isv(sw0, EW_R) and isv(sw1, EW_R):
        print("  HOST L NEVER REACHES samplesOut[0]: both TX sites see R's word."
              "\n  The fault is upstream of the tap -- decouple -> mixer -> audiohub"
              "\n  (GiveSamplesToDevice / DoSampleTransfer). Product bug.")
    elif sw1 == 0:
        print("  SITE 1 NEVER RAN: slot 1 keeps a stale/zero word, so the R capture"
              "\n  is whatever was there before. The L==R observation was an"
              "\n  artifact of that.")
    else:
        print(f"  UNEXPECTED combination: site=({fmt(sw0)},{fmt(sw1)})"
              f" tx=({fmt(tx0)},{fmt(tx1)}). Do not summarise -- get more data.")
    a.snd_pcm_close(play)
    a.snd_pcm_close(cap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
