#!/usr/bin/env python3
"""Where are the 36 % zero frames, and what is their period? Names the mechanism.

Established, so not re-litigated here:
  - the rig is clean: single-threaded duplex sustains out 99.99 % / in 99.54 %
    with 0 errors, and fill is 0.6360 at rings of 8 K, 32 K and 128 K -- so it is
    not pacing, not buffer size, not thread contention;
  - with the tap DISARMED fill is 0.0000 (that is the real ADC with no mic), and
    the injected words are always nonzero -- so a zero capture frame can only
    mean take() returned its fallback, i.e. the injection did not run that frame;
  - channels are distinct (L==R on 0.0003..0.0012 of nonzero pairs), so there is
    no mono collapse; the earlier "L==R on 100%" was zeros scoring as agreement.

So the open question is the SHAPE of the missing 36 %. Two candidate mechanisms
with opposite fixes:
  (a) periodic stride -- every k-th frame is missed. That says the RX injection
      is compiled into only some of the half-frames, or that the USB capture
      re-reads one held word across consecutive frames;
  (b) long runs -- contiguous blocks of zeros. That says the capture path
      delivers in bursts and one half of each burst is stale/fallback.
Reported: zero-run histogram, the shortest run, and the mean zero fraction at
each phase mod 2..8 (a stride shows as a phase hitting ~1.0 and others ~0).

Counters are CLEARED immediately before the window and read immediately after
the streams close, so every number describes exactly this run -- an uncleared
counter was what made the previous three legs' witness values uninterpretable.
"""
import ctypes
import sys
import time
from collections import Counter

sys.path.insert(0, "rig")

WORD_L = 0x55AA5A
WORD_R = 0x0F0F01
SECS = 1.5


def scpi(cmd):
    import subprocess
    r = subprocess.run(
        [".venv/bin/python", "csd_diag.py", "--backend", "rawmidi",
         "--timeout", "15", cmd],
        capture_output=True, text=True, timeout=40)
    return (r.stdout or "").strip().replace("\r", "")


def main():
    import os
    import numpy as np
    import csd_signal as sig
    # Rate is an ARGUMENT: does the put/take ratio stay 1.0 below some LRCLK
    # rate and fall above it (a software throughput ceiling on the audio core),
    # or is it rate-independent (something else)? csd_signal.RATE is module-level
    # and must be re-pointed per leg -- a schedule synthesised at 384 kHz and
    # played at 192 kHz is a different signal, and every earlier off-rate leg
    # that forgot this misreported the device rate as the base rate.
    R = int(os.environ.get("CSD_RATE", sig.RATE))
    sig.RATE = R
    print(f"RATE under test: {R:,}")

    a = ctypes.CDLL("libasound.so.2")
    for fn, rt, at in (
            ("snd_pcm_writei", ctypes.c_long,
             [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
            ("snd_pcm_readi", ctypes.c_long,
             [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
            ("snd_pcm_avail_update", ctypes.c_long, [ctypes.c_void_p])):
        getattr(a, fn).restype = rt
        getattr(a, fn).argtypes = at

    def op(st, bufsz=32768):
        p = ctypes.c_void_p()
        assert a.snd_pcm_open(ctypes.byref(p), b"hw:3,0", st, 0) >= 0
        hp = ctypes.c_void_p()
        a.snd_pcm_hw_params_malloc(ctypes.byref(hp))
        a.snd_pcm_hw_params_any(p, hp)
        a.snd_pcm_hw_params_set_access(p, hp, 3)
        a.snd_pcm_hw_params_set_format(p, hp, 10)
        a.snd_pcm_hw_params_set_channels(p, hp, 2)
        d = ctypes.c_uint(R)
        a.snd_pcm_hw_params_set_rate_near(p, hp, ctypes.byref(d), None)
        b = ctypes.c_uint(bufsz)
        a.snd_pcm_hw_params_set_buffer_size_near(p, hp, ctypes.byref(b))
        per = ctypes.c_uint(min(bufsz // 4, 4096))
        a.snd_pcm_hw_params_set_period_size_near(p, hp, ctypes.byref(per), None)
        assert a.snd_pcm_hw_params(p, hp) >= 0
        a.snd_pcm_prepare(p)
        return p

    # Runtime lever A/B on ONE image: preset 0 makes csd_dsp_frame return early
    # ("byte-exact bypass"), removing the FIR from the audio core without a
    # rebuild. If the put-ratio deficit moves with it, the DSP is what stalls the
    # I2S loop for 96 frames at a time.
    preset = int(sys.argv[sys.argv.index("--dsp") + 1]) if "--dsp" in sys.argv else 2
    # ALSA's set_rate_near is BEST-EFFORT: it returns the nearest supported rate,
    # it does not fail on a mismatch. A leg that silently ran at a granted rate
    # other than the requested one would put the whole ladder in the wrong place,
    # so read back what the kernel actually configured, from its own record.
    def granted_rate(is_play):
        # A full-duplex UAC2 card is ONE pcm device: playback is pcm0p, capture
        # is pcm0c. The old version passed the stream index into the pcm number
        # and asked for pcm1c, which does not exist -- so capture read None and
        # every leg falsely warned "this leg measured a different rate".
        for path in (f"/proc/asound/card3/pcm0{'p' if is_play else 'c'}/sub0/hw_params",
                     f"/proc/asound/card3/pcm1{'p' if is_play else 'c'}/sub0/hw_params"):
            try:
                txt = open(path).read()
                break
            except OSError:
                continue
        else:
            return None
        for tok in txt.replace(":", " ").split():
            if tok.isdigit() and 7000 <= int(tok) <= 1_536_000:
                return int(tok)
        return None

    print("IDN:", scpi("*IDN?"))
    scpi(f"DIAG:DSP:PRESet {preset}")
    got = scpi("DIAG:DSP:PRESet?")
    print(f"DSP : {got}  (asked {preset})")
    if got.split(",")[0].strip() != str(preset):
        print("ABORT: preset did not adopt")
        return 2
    print("arm :", scpi("DEBUG:LOOPback 1"))
    print("clear:", scpi("DEBUG:LOOPback:CLEAR"))

    n = int(R * SECS)
    sched = np.zeros((n, 2), dtype="<u4")
    sched[:, 0] = np.uint32(WORD_L) << np.uint32(8)
    sched[:, 1] = np.uint32(WORD_R) << np.uint32(8)
    base = sched.ctypes.data

    play, cap = op(0), op(1)
    # --no-capture opens the PLAYBACK stream only. Why that isolates something:
    # put/take are incremented by the audio core itself and need no ADC path, so
    # the LRCLK rate is measurable without capture -- and the capture direction is
    # the other half of the per-frame cross-tile channel traffic on c_aud (tile[1]
    # audio core <-> tile[0] decouple, which also carries XUD at 32/32 chanends).
    # If the LRCLK deficit disappears without capture, the audio core was blocking
    # on tile[0] servicing the ISO endpoints, not running out of cycles -- which is
    # the reading the mixer/DSP A/Bs forced, since removing work from the audio
    # core did not fix it and removing the mixer made the census WORSE.
    no_cap = "--no-capture" in sys.argv
    if no_cap:
        a.snd_pcm_close(cap)
        cap = None
        print("capture stream: CLOSED (playback-only leg)")
    a.snd_pcm_start(play)
    if cap is not None:
        a.snd_pcm_start(cap)
    g_out = granted_rate(True)
    g_in = granted_rate(False) if cap is not None else None
    print(f"kernel-granted rates: playback={g_out}  capture={g_in}"
          + ("" if (g_out == R and (g_in == R or cap is None)) else
             f"   <-- NOT {R:,}: this leg measured a different rate"))

    # Feedback-endpoint value, read from the kernel while the stream runs. This
    # build carries -DUAC_FORCE_FEEDBACK=1, so the number it reports is a forced
    # constant rather than a measurement of what the device consumes -- which is
    # precisely why the host never throttled despite the I2S loop running slow.
    # Printed so the masking is visible in the same report as the put ratio,
    # instead of being remembered as a footnote that invalidated an earlier
    # "device rate 383,963 Hz" claim.
    def feedback_value():
        for sub in ("stream0", "pcm0c/sub0/hw_params", "pcm0p/sub0/hw_params"):
            try:
                txt = open(f"/proc/asound/card3/{sub}").read()
            except OSError:
                continue
            for ln in txt.splitlines():
                if "Momentary freq" in ln or "feedback" in ln.lower():
                    return ln.strip()
        return None

    rbuf = (ctypes.c_char * (4096 * 8))()
    got = []
    woff = 0
    errs = 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < SECS:
        did = False
        av = a.snd_pcm_avail_update(play)
        if av >= 512 and woff < n:
            k = min(av, 4096, n - woff)
            r = a.snd_pcm_writei(play, base + woff * 8, k)
            if r < 0:
                if a.snd_pcm_recover(play, r, 1) < 0:
                    errs += 1; break
            else:
                woff += r; did = True
        if cap is not None:
            av = a.snd_pcm_avail_update(cap)
            if av >= 512:
                k = min(av, 4096)
                r = a.snd_pcm_readi(cap, rbuf, k)
                if r <= 0:
                    if a.snd_pcm_recover(cap, r if r < 0 else -1, 1) < 0:
                        errs += 1; break
                else:
                    got.append(bytes(rbuf[:r * 8])); did = True
        if not did:
            time.sleep(0.0002)
    el = time.monotonic() - t0
    # Snapshot the tap counters BEFORE closing the streams. The audio() task
    # free-runs on underflowWord once the host stream stops -- there is no
    # "stream stopped" signal into that loop -- so counters read after close
    # include however long the SCPI round-trip and teardown took, which made the
    # put-derived rate read 1.48-1.76x the negotiated rate: impossible for a
    # hardware-clocked port, and the tell that the metric was invalid. Read the
    # witness while the streams are still live; an earlier test showed a mid-stream
    # rawmidi query does not perturb fill (0.6359 vs 0.6359), so this is free.
    witness_early = scpi("DEBUG:LOOPback:INDEX?")
    a.snd_pcm_close(play)
    if cap is not None:
        a.snd_pcm_close(cap)

    # Read AFTER close: a rawmidi query mid-stream costs tens of ms against a
    # ~21 ms ring. Counters persist, so this is free and describes the window.
    # LRCLK-delta census from the device itself: frames whose measured delta fell
    # outside the 32-BCLK budget, split by fast/slow. Present only in a build
    # where csd_mb_note_i2s_delta is compiled (i.e. NOT release) -- in a release
    # build it is stubbed to nothing and would report a clean 0%, which is worse
    # than no number. Read here because the put/take ratio can only say frames
    # were missing, not whether the loop missed a deadline or blocked.
    i2s = scpi("DEBUG:I2S?")
    print(f"DEBUG:I2S? (frames,bad,hi,lo,first_bad): {i2s}")
    idx = scpi("DEBUG:LOOPback:INDEX?")
    try:
        v = [int(x) for x in idx.split(",")]
        assert len(v) == 21
    except (ValueError, AssertionError):
        print(f"INDEX? unparsable: {idx!r}")
        return 2
    put0, put1 = v[0], v[1]
    tak0, tak1 = v[4], v[5]
    s0, s1 = v[8] + v[9], v[12] + v[13]

    frames_written = woff
    exp_calls = 2 * frames_written          # 2 channels per frame, both sites
    print(f"\nwindow {el:.3f} s   host wrote {frames_written:,} frames "
          f"({frames_written/el:,.0f} f/s = {100*frames_written/el/R:.2f}%)")
    fb = feedback_value()
    print(f"kernel feedback: {fb}")

    # THE number. puts/channel is incremented once per LRCLK frame per channel by
    # the audio core itself, so puts/elapsed IS the LRCLK rate the device actually
    # produced -- independent of USB pacing, of the forced feedback endpoint, and
    # of ALSA's grant. Compare against R: if it lands well below R, the device was
    # never running at the negotiated rate and every 'device rate 383,963 Hz' claim
    # (which came from the USB feedback path) was measuring the host, not the box.
    lrclk_hz = put0 / el
    print(f"EFFECTIVE LRCLK = {lrclk_hz:,.0f} Hz   (negotiated {R:,} -> "
          f"{100*lrclk_hz/R:.2f}%)")
    print(f"  USB side wrote at {frames_written/el:,.0f} f/s; the I2S loop consumed "
          f"{lrclk_hz:,.0f} f/s. The gap is what the capture reports as zeros.")
    print(f"device puts: ch0={put0:,} ch1={put1:,}   "
          f"ratio to 2x-frames = {(put0+put1)/exp_calls:.4f}")
    print(f"device takes: ch0={tak0:,} ch1={tak1:,}  "
          f"ratio to 2x-frames = {(tak0+tak1)/exp_calls:.4f}")
    print(f"put sites: site0={s0:,} site1={s1:,}   build={v[16]}")

    if not got:
        print("NO CAPTURE -- void")
        return 2
    f = np.concatenate([np.frombuffer(c, dtype="<u4") for c in got])
    L = (f[0::2] >> np.uint32(8)).astype(np.int64)
    Rr = (f[1::2] >> np.uint32(8)).astype(np.int64)
    zero = ((L == 0) & (Rr == 0)).astype(np.int8)
    nz = int((~zero.astype(bool)).sum())
    print(f"\ncaptured {len(L):,} frames  nonzero {nz:,}  fill={nz/len(L):.4f}"
          f"  zero={1-nz/len(L):.4f}")

    # Run-length structure of the zeros.
    d = np.diff(np.concatenate(([0], zero, [0])))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    runs = ends - starts
    if len(runs):
        c = Counter(int(x) for x in runs)
        print(f"zero runs: {len(runs):,}  shortest={runs.min()}  "
              f"longest={runs.max()}  mean={runs.mean():.2f}")
        print("  most common run lengths:",
              ", ".join(f"{k}x{n}" for k, n in c.most_common(8)))

    # Phase test: a periodic stride shows up as one phase near 1.0.
    print("  zero fraction by phase (mod k):")
    for k in (2, 3, 4, 5, 6, 8, 12):
        m = len(L) - (len(L) % k)
        ph = zero[:m].reshape(-1, k).mean(axis=0)
        line = " ".join(f"{p:.3f}" for p in ph)
        flag = ""
        if ph.max() > 0.9 and ph.min() < 0.1:
            flag = "   <-- PERIODIC STRIDE: every " + \
                   ",".join(str(int(i)) for i in np.flatnonzero(ph > 0.9)) + \
                   f"-of-{k} frame is missed"
        print(f"    k={k:<3} {line}{flag}")

    # Where do the zeros sit relative to the start, and do they cluster in time?
    blocks = 20
    bl = len(zero) // blocks
    dens = [float(zero[i*bl:(i+1)*bl].mean()) for i in range(blocks)]
    print(f"  zero density over time (20 blocks): "
          f"min={min(dens):.3f} max={max(dens):.3f}")
    print(f"    {' '.join(f'{d:.2f}' for d in dens)}")
    scpi("DEBUG:LOOPback 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
