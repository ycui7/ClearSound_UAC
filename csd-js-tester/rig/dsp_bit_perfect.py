#!/usr/bin/env python3
"""DSP bit-perfectness: host stream -> DAC -> I2S -> ADC loopback, compared
against the SAME integer math the XMOS runs.

Directive 2026-11-04. This is the preset-2 twin of one_shot_block.py: the tap
(xua_audiohub csd_mb_loop_put_site, in the TX half of the audio() loop)
records samplesOut AFTER UserBufferManagement -> csd_dsp_frame rewrote it in
place (csd_dsp.xc:616), so the capture is the FILTERED stream. A PASS is a
compound claim -- path bit-exact AND this port bit-exact -- so a failure is
reported with both suspects, never as a bare FAIL.

THE MATH, ported from the asm (lib_audio_dsp/src/dsp/biquad.S), not from a
float model:
    vsetc 12          -> VPU round mode: each product is (x*c + 2**29) >> 30
    vlmaccr state[0]  -> accumulate the rounded products in an int40
    vstrpv            -> SATURATING store extract into state[3] (y history)
    shl sample, lsh   -> our table has lsh=0 at every rate, so identity
                         (asserted: a non-zero lsh must be re-derived, not
                         inherited); the saturate is the only clamp
    state: x[n] x[n-1] x[n-2] y[n-1] y[n-2], per channel, zeroed by
           csd_dsp_bq_config (memset) at stream start -- independent L/R,
           bq_state[2][8].
The vendor's own python model (audio_dsp/dsp/biquad.py::process_xcore with
utils.vlmaccr/vpu_mult) implements exactly this per-product rounding, which is
NOT the form in process_int (single round after summing). Choosing wrongly
would silently produce a plausible-but-wrong expectation, so BOTH forms are
implemented and each run names the one it used. Anchoring the choice to the
vendor's silicon-validated model is the point; the A/B is reported.

DETAILS THAT BITE (each verified in the device source, not assumed)
  - Wire scaling: pack_frames puts the 24-bit field in bits 31..8, so the DSP
    sees int32 = v24 << 8 (signed). We filter THAT, and compare the FULL
    32-bit captured word -- not the 24-bit unpack -- so a mismatch in the low
    8 bits cannot hide behind the reader's >>8.
  - DoP bypass: csd_dsp_frame returns byte-exact once it latches DoP (4
    consecutive frames where BOTH channels satisfy DOP_MARK). A latched
    stream would score unfiltered output against a filtered expectation. The
    schedule is scanned and the run aborts before playing if it can latch.
  - Preset lifecycle: a change calls csd_dsp_restart -> bq_state memset. The
    preset is therefore posted BEFORE the streams open and never touched
    again, so state zeroes exactly once, at stream start, as the model does.
  - Mixer build (MAX_MIX_COUNT=0, generator OFF): the pre/post hooks are
    byte-exact passthrough, so the schedule reaching csd_dsp_frame is the
    schedule we filter -- guaranteed by arm_device's gates (trim 0, gen off,
    mixer unity, codec 0 dB/unmute, tap armed).
"""
import argparse
import gc
import json
import os
import sys
import threading
import time

import numpy as np

import align_score as A
import csd_signal as sig
import one_shot_block as O
from bit_perfect_noise import (CHUNK_FRAMES, MARKER_LEN, MARKER_PERIOD, alsa,
                               open_pcm, pack_frames, unpack_frames)

I32MIN, I32MAX = -(1 << 31), (1 << 31) - 1

# The device's own table, csd_dsp_bq_coeffs.h (rate, coeffs, lsh).
BQ = {
    44100:  ([539841997, -1068049528, 528331572, 2131100700, -1057482917], 0),
    48000:  ([539703980, -1068721454, 529122242, 2132431779, -1058794723], 0),
    88200:  ([538993196, -1072178116, 533216048, 2139292081, -1065581385], 0),
    96000:  ([538924288, -1072512895, 533614890, 2139957641, -1066242101], 0),
    176400: ([538569281, -1074236684, 535675200, 2143387853, -1069653826], 0),
    192000: ([538534852, -1074403773, 535875503, 2143720635, -1069985394], 0),
    352800: ([538357446, -1075264521, 536909026, 2145435749, -1071695876], 0),
    384000: ([538340238, -1075347991, 537009399, 2145602141, -1071861964], 0),
}


def _sat32(v):
    if v < I32MIN:
        return I32MIN
    if v > I32MAX:
        return I32MAX
    return v


def _int34(v):
    """Vendor utils.int34 range check: the VPU product after >>30 lives in an
    int34. Out of range means the model or the coefficients are wrong."""
    if v > (1 << 33) - 1 or v < -(1 << 33):
        raise OverflowError(f"VPU product {v} escaped int34 -- model invalid")
    return v


def biquad_stream(x, coeffs, lsh, rounding):
    """Direct form I, one channel, int32 in / int32 out.

    rounding="vpu"  : (x*c + 2**29) >> 30 PER PRODUCT, summed in int40, then
                      saturating store -- the vlmaccr-under-vsetc-12 form the
                      asm runs and audio_dsp::process_xcore models. FAITHFUL.
    rounding="once" : full 64-bit products summed, ONE round+shift at the end
                      (audio_dsp::process_int form). Kept only as the A/B that
                      tells which semantics the silicon implements.
    """
    c0, c1, c2, c3, c4 = coeffs
    r = 1 << 29
    out = [0] * len(x)
    x1 = x2 = y1 = y2 = 0
    if rounding == "vpu":
        for i in range(len(x)):
            xv = x[i]
            acc = (_int34((xv * c0 + r) >> 30) + _int34((x1 * c1 + r) >> 30)
                   + _int34((x2 * c2 + r) >> 30) + _int34((y1 * c3 + r) >> 30)
                   + _int34((y2 * c4 + r) >> 30))
            if acc > (1 << 39) - 1 or acc < -(1 << 39):
                raise OverflowError("accumulator escaped int40")
            y = _sat32(acc)
            out[i] = y
            x2, x1, y2, y1 = x1, xv, y1, y
    else:
        for i in range(len(x)):
            xv = x[i]
            acc = (xv * c0 + x1 * c1 + x2 * c2 + y1 * c3 + y2 * c4 + r) >> 30
            y = _sat32(acc)
            out[i] = y
            x2, x1, y2, y1 = x1, xv, y1, y
    if lsh:
        out = [_sat32(v << lsh) for v in out]
    return out


def dop_would_latch(wL, wR):
    """csd_dsp.xc DOP_MARK, 4 consecutive frames on BOTH channels.

    Pass the words the DEVICE sees (int32 on the wire = v24 << 8), NOT the
    24-bit fields: DOP_MARK tests bytes 0 and 2 of the word the DSP receives
    (csd_dsp.xc:93), and the wire codec parks the 24-bit value in bits 31..8,
    so byte0 is always 0 -- for THIS rig's stimulus the latch is structurally
    impossible (proven in rig/dsp_model_selftest.py). Kept as a live guard
    anyway: if the wire format ever changes, this stops the run instead of
    silently scoring a bypassed FIR against a filtered expectation."""
    def mark(s):
        b0 = s & 0xFF
        b2 = (s >> 16) & 0xFF
        return b0 == b2 and (b0 == 0x05 or b0 == 0xFA)
    run = 0
    for i in range(len(wL)):
        if mark(wL[i]) and mark(wR[i]):
            run += 1
            if run >= 4:
                return i - 3
        else:
            run = 0
    return None


def derive_sync(uL, uR, rate, need=1024):
    """The aligner finds the stream head by hunting a constant sync run of
    min_run frames -- the rig's SYNC_L/SYNC_R marker blocks. Under preset 2
    the markers are FILTERED too, so the values must come from the filtered
    schedule, and the shelf's poles sit near z=0.9987 (a2/a0 =
    -coeffs[4]/2**30), i.e. a ~770-sample time constant: whether a 4096-frame
    marker block settles to an EXACTLY constant integer is a measured
    question, not an assumption. Returns (syncL, syncR, marker_start, runlen)
    for the first marker block with a shared constant run >= need, else None.
    A None here means the aligner cannot anchor and the run must not be
    scored at all."""
    n_marker = MARKER_LEN
    for m0 in range(MARKER_PERIOD, min(len(uL), 16 * MARKER_PERIOD), MARKER_PERIOD):
        if m0 + n_marker > len(uL):
            break
        segL, segR = uL[m0:m0 + n_marker], uR[m0:m0 + n_marker]
        best = (0, None, None)
        i = 0
        while i < n_marker:
            j = i
            while (j + 1 < n_marker and segL[j + 1] == segL[i]
                   and segR[j + 1] == segR[i]):
                j += 1
            if j - i + 1 > best[0]:
                best = (j - i + 1, int(segL[i]), int(segR[i]))
            i = j + 1
        if best[0] >= need:
            return best[1], best[2], m0, best[0]
    return None


def filtered_expectation(uL, uR, rate, rounding):
    """int32 words the DSP will see (v24 << 8, signed) -> filtered int32 words.
    Cached: the loop is sequential (IIR), so re-running costs minutes."""
    coeffs, lsh = BQ[rate]
    # .npy, not JSON: 2 x 23M int64 is ~370 MB and json would take minutes
    # and roughly three times the space to say the same thing.
    cache = (f"/tmp/dsp_expect_{rate}_{rounding}_{len(uL)}.npy")
    if os.path.exists(cache):
        arr = np.load(cache)
        return arr[0], arr[1]
    if rounding != "vpu":
        # The A/B form exists only for the selftest's discrimination check;
        # hardware runs always use the faithful one, which the selftest pins
        # to the vendor's VPU model bit-for-bit.
        sys.exit("rounding='once' is a selftest-only discriminator; the "
                 "hardware verdict must come from the faithful 'vpu' form")
    here = os.path.dirname(os.path.abspath(__file__))
    exe = os.path.join(here, "dsp_bq_host")
    if not os.path.exists(exe):
        sys.exit(f"missing {exe} -- build it: gcc -O2 -o dsp_bq_host "
                 f"dsp_bq_host.c (see the file header for why it is C)")
    # int32 end to end. The first version used Python int lists over 23M
    # frames (~1 GB each) and was cgroup-OOM-killed; int32 is both exact and
    # 8x smaller, and the C twin takes/returns raw int32 pairs.
    t0 = time.monotonic()
    # Wire scaling: pack_frames parks the 24-bit field in bits 31..8, so the
    # word the DSP filters is (v24 << 8) as int32.
    sL = (uL.astype(np.uint32) << np.uint32(8)).astype(np.int32)
    sR = (uR.astype(np.uint32) << np.uint32(8)).astype(np.int32)
    pairs = np.empty((len(sL), 2), dtype=np.int32)
    pairs[:, 0] = sL
    pairs[:, 1] = sR
    del sL, sR
    inf = cache + ".in.bin"
    outf = cache + ".out.bin"
    pairs.tofile(inf)
    del pairs
    import subprocess
    cmd = [exe, inf, outf] + [str(c) for c in coeffs] + [str(lsh)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        sys.exit(f"dsp_bq_host failed rc={r.returncode}: {r.stderr.strip()}")
    print(f"  {r.stderr.strip()}")
    out = np.fromfile(outf, dtype=np.int32).reshape(-1, 2)
    if len(out) != len(uL):
        sys.exit(f"dsp_bq_host returned {len(out)} frames, expected {len(uL)}")
    print(f"  host biquad twin over {len(uL):,} frames x2 ch "
          f"in {time.monotonic()-t0:.1f}s")
    eL = np.ascontiguousarray(out[:, 0])
    eR = np.ascontiguousarray(out[:, 1])
    np.save(cache, np.stack([eL, eR]))
    os.remove(inf); os.remove(outf)
    return eL, eR



def find_delta(cL, cR, uL, uR, W=8192, SPAN=8192):
    """Offset search for a filtering preset: returns (delta, matched_frames).

    The rig's aligner anchors on a constant sync run, which a recursive filter
    cannot provide (poles at |z|=0.99866 -> ~16,000-sample decay, longer than
    the 4096-frame marker; rounding can also leave a last-bit limit cycle).
    Anchor instead on the stimulus's own property: every frame is unique
    band-limited noise on an independent seed, so exactly one offset matches
    thousands of frames. Safety does not rest on this being right -- the
    verdict comes from scoring EVERY frame afterwards, so a wrong offset
    produces a huge defect count, never a false PASS.
    """
    got_n, n_frames = len(cL), len(uL)
    # Nominal: capture frame i corresponds to schedule frame i + delta. The
    # sign is DATA, not convention -- the proven preset-0 rig measured
    # delta = -236 on this DAC, so the search must span both signs (an
    # earlier draft refused d < 0 and would have VOIDed good runs). The
    # synthetic known-offset tests in rig/dsp_model_selftest.py pin the
    # positive side; the hardware run exercises the negative side.
    base = n_frames - got_n
    best = (-1, None)
    for d in range(base - SPAN, base + SPAN + 1):
        i0 = max(0, -d)                     # first capture index in range
        i1 = min(got_n, n_frames - d)       # one past the last
        if i1 - i0 < W:
            continue
        m = int(np.count_nonzero(
            (cL[i0:i0 + W] == uL[i0 + d:i0 + d + W])
            & (cR[i0:i0 + W] == uR[i0 + d:i0 + d + W])))
        if m > best[0]:
            best = (m, d)
    return best


def build_regions(cL, cR, uL, uR, delta, PROBE=384):
    """Contiguous runs where the expectation still matches, probed every
    PROBE frames -- a single dropped ISO packet at 384 kHz is 48 frames, so
    probes must stay well above that (same rule as align_score.PROBE_STRIDE).
    An unprovable stretch is left OUT of the regions: the rig never scores
    frames it cannot see, and never guesses across a gap."""
    got_n = len(cL)
    # Start at the first capture index whose schedule partner exists for THIS
    # offset (negative delta means the first -delta capture frames map before
    # the block and can never be proven).
    lo = max(0, got_n - len(uL), -delta)
    regions = []
    cur = lo
    while cur < got_n:
        end = cur
        p = cur + PROBE
        while (p + PROBE <= got_n and p + delta + 64 <= len(uL)
               and p + delta >= 0):
            mm = int(np.count_nonzero(
                (cL[p:p + 64] == uL[p + delta:p + delta + 64])
                & (cR[p:p + 64] == uR[p + delta:p + delta + 64])))
            if mm != 64:
                break
            end = p + 64
            p += PROBE
        if end > cur + PROBE:
            regions.append((cur, end, delta))
        cur = (end + PROBE) if end > cur else (cur + PROBE)
    return regions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=1.0)
    ap.add_argument("--rounding", choices=["vpu", "once"], default="vpu")
    ap.add_argument("--mixer", action="store_true")
    ap.add_argument("--preset", type=int, default=2)
    a = ap.parse_args()

    rate = sig.RATE
    coeffs, lsh = BQ[rate]
    print(f"=== DSP bit-perfect: preset {a.preset}, rate={rate}, lsh={lsh}, "
          f"rounding={a.rounding} ===")
    print(f"  coeffs = {coeffs}")
    if a.preset == 2 and lsh != 0:
        sys.exit("lsh != 0 changes the shl/overflow semantics -- the port must "
                 "be re-derived from the asm before this run means anything")

    n_frames = ((int(rate * 60 * a.minutes) + CHUNK_FRAMES - 1)
                // CHUNK_FRAMES) * CHUNK_FRAMES
    expL, expR, block = O.build_block(n_frames)
    print(f"  block {n_frames:,} frames, markers every {MARKER_PERIOD:,}")

    # Vectorised: a Python list over 23M frames cost ~1 GB and is what got
    # the first version OOM-killed. Words are u32; byte0 is 0 by construction
    # (wire parks the value in bits 31..8), which is the property
    # dsp_model_selftest.py asserts -- but the guard must still be a real
    # scan, because it is the only thing standing between a wire-format
    # change and a silently bypassed FIR.
    wL32 = ((expL.astype(np.uint32) << np.uint32(8)) & np.uint32(0xFFFFFFFF))
    wR32 = ((expR.astype(np.uint32) << np.uint32(8)) & np.uint32(0xFFFFFFFF))
    def _mark(w):
        b0 = (w & np.uint32(0xFF))
        b2 = ((w >> np.uint32(16)) & np.uint32(0xFF))
        return (b0 == b2) & ((b0 == np.uint32(0x05)) | (b0 == np.uint32(0xFA)))
    both = _mark(wL32) & _mark(wR32)
    idx = np.flatnonzero(both)
    run, p = 1, None
    for k in range(1, len(idx)):
        if idx[k] == idx[k - 1] + 1:
            run += 1
            if run >= 4:
                p = int(idx[k] - 3); break
        else:
            run = 1
    if p is not None:
        sys.exit(f"ABORT: schedule would latch DoP at frame {p:,}; the FIR "
                 "bypasses and the comparison is meaningless. Change stimulus.")
    print("  DoP scan (on the words the DSP receives): no 4-frame latch -> "
          "FIR stays armed for the whole run")
    del wL32, wR32, both

    if a.preset == 0:
        sys.exit("preset 0 is one_shot_block.py's job (byte-exact bypass); "
                 "this rig exists for the filtering presets")

    # Feed the twin the 24-bit schedule fields directly (it scales by 256
    # itself); int32 keeps four 23M-element arrays at ~90 MB each instead of
    # ~180 MB. build_block's int64 copies are freed once the twin has run.
    eL, eR = filtered_expectation(expL.astype(np.int32), expR.astype(np.int32),
                                  rate, a.rounding)
    # >>8, NOT &0xFFFFFF: unpack_frames returns (wire_word >> 8), and the tap
    # hands us the filtered word as the wire word, so the matching 24-bit view
    # of the expectation is the TOP 24 bits of the filtered word. (Masking the
    # low bits instead compares bits 23..0 against bits 31..8: the anchor
    # would match nothing and the run would VOID loudly rather than lie, but
    # it would measure nothing.)
    uL = (eL.astype(np.uint32) >> np.uint32(8)).astype(np.int32)
    uR = (eR.astype(np.uint32) >> np.uint32(8)).astype(np.int32)
    del expL, expR                       # 736 MB: nothing downstream needs it
    gc.collect()
    # int(v) first: numpy refuses to cast the python mask 0xFFFFFFFF into an
    # int32 scalar (OverflowError), which is what killed run #2 after the
    # 23M-frame filter had already succeeded.
    print(f"  first 3 filtered words L: {[hex(int(v) & 0xFFFFFFFF) for v in eL[:3]]}")

    # ---- device: all gates first, THEN the preset (so bq_state zeroes once) -
    O.arm_device(mixer_declared=a.mixer)
    O.require(f"DIAG:DSP:PRESet {a.preset}", what="DSP preset posted pre-stream")
    got = O.scpi("DIAG:DSP:PRESet?") if hasattr(O, "scpi") else None
    if got is None:
        import subprocess
        o = subprocess.run([".venv/bin/python", "csd_diag.py", "--backend",
                            "rawmidi", "--timeout", "8", "DIAG:DSP:PRESet?"],
                           capture_output=True, text=True, timeout=20).stdout
        got = o.strip().splitlines()[0].strip().strip('"') if o.strip() else ""
    if got.split(",")[0].strip() != str(a.preset):
        sys.exit(f"DIAG:DSP:PRESet? -> {got!r}, not {a.preset} -- refusing")
    print(f"  DIAG:DSP:PRESet? -> {got}")

    O.arm_tap("dsp tap")
    tx_before = O.read_tx_frames()
    print(f"  witness: tx_frames before run = {tx_before}")

    pd, play_per, play_buf = open_pcm(0, "playback")
    cd, cap_per, cap_buf = open_pcm(1, "capture")
    trw = O.AlsaXrunTracker(pd, True, "playback")
    trr = O.AlsaXrunTracker(cd, False, "capture")
    stop = threading.Event()
    write_done = threading.Event()
    captured = []

    prefill_frames = min(play_buf // 2, n_frames)
    wrote = O.prefill(pd, trw, block, prefill_frames)
    print(f"  prefilled {wrote:,} frames, starting capture")
    if alsa.snd_pcm_start(cd) < 0:
        sys.exit("capture start failed -- VOID")
    th_r = threading.Thread(target=O.reader,
                            args=(cd, trr, captured, n_frames + 4 * CHUNK_FRAMES,
                                  stop, a.minutes * 60 + 120, write_done),
                            daemon=True)
    th_r.start()
    time.sleep(0.25)
    alsa.snd_pcm_start(pd)

    def _run_writer():
        O.writer(pd, trw, block, n_frames, stop, wrote)
        write_done.set()

    tw = threading.Thread(target=_run_writer, daemon=True)
    tw.start()
    tw.join()
    th_r.join()

    print("  " + trw.summary())
    print("  " + trr.summary())
    tx_after = O.read_tx_frames()
    adv = tx_after - tx_before if (tx_after is not None and tx_before is not None) else None
    print(f"  witness: tx_frames after = {tx_after} (advanced {adv})")
    alsa.snd_pcm_close(pd)
    alsa.snd_pcm_close(cd)
    if adv is None or adv <= 0:
        sys.exit("VOID -- tap did not advance; the capture proves nothing")
    if not (trw.clean and trr.clean):
        sys.exit("VOID -- ALSA lost frames; a defect rate here would be the "
                 "tester's, not the device's")

    cap = b"".join(captured)
    got_n = len(cap) // O.FRAME_BYTES
    cL, cR = unpack_frames(cap[:got_n * O.FRAME_BYTES])
    print(f"  captured {got_n:,} frames")

    # ---- anchor + regions (unit-tested in rig/dsp_model_selftest.py) ------
    frac, delta = find_delta(cL, cR, uL, uR)
    if delta is None:
        sys.exit("VOID: no candidate offset in the search window at all")
    if frac < 8192:
        # A partial match is diagnostic, not a verdict: close-but-wrong means
        # the host port is ALMOST right (wrong rounding semantics -- try
        # --rounding once), while a scattered low count means the capture
        # never held the stream. Both are loud failures, never a PASS.
        sys.exit(f"VOID: best offset matched only {frac}/8192 probe frames "
                 f"(delta={delta}). Near-miss => suspect the host port "
                 f"(--rounding once); scattered low => suspect the capture "
                 f"path. one_shot_block.py at preset 0 is the control that "
                 f"separates the two.")
    print(f"  anchor: delta={delta:,} matched {frac}/8192 probe frames exactly")
    regions = build_regions(cL, cR, uL, uR, delta)
    scored = sum(b - x for (x, b, _) in regions)
    print(f"  regions: {len(regions)}, {scored:,} of {got_n:,} frames "
          f"scoreable ({100.0*scored/max(1,got_n):.1f}%)")
    if scored < 0.9 * got_n:
        print("  WARNING: over 10% unprovable -- read as a delivery problem, "
              "not a DSP problem")
    if not regions:
        sys.exit("VOID: no scoreable region")

    lookup = O.sent_lookup(uL, uR)
    sc = A.score(cL, cR, lookup, regions)
    print(f"\n=== 24-bit score ===")
    print(f"frames checked : {sc['checked']:,}")
    print(f"defects        : {sc['defects']}  rate="
          f"{sc['defects']/max(1,sc['checked']):.3e}")

    # ---- strict: full 32-bit words, so low bits cannot hide behind >>8 -----
    w = np.frombuffer(cap[:got_n * O.FRAME_BYTES], dtype="<u4")
    wL = w[0::2]                                  # uint32 view, no copy
    wR = w[1::2]
    strict_bad = 0
    strict_checked = 0
    first_bad = None
    for (first, last, d_off) in regions:
        # Both bounds: numpy WRAPS a negative index and would silently compare
        # the end of the schedule as if it were valid.
        last = min(last, len(eL) - d_off, len(eL))
        first = max(first, -d_off, 0)
        if last <= first:
            continue
        idx = np.arange(first, last, dtype=np.int64)
        e_l = eL[idx + d_off]
        e_r = eR[idx + d_off]
        g_l = wL[first:last]
        g_r = wR[first:last]
        m = (g_l != e_l.astype(np.uint32)) | (g_r != e_r.astype(np.uint32))
        strict_checked += int(m.size)
        strict_bad += int(m.sum())
        if first_bad is None and m.any():
            j = int(np.argmax(m))
            print(f"  first strict mismatch at capture {first+j:,} "
                  f"(schedule {first+j+d_off:,}): "
                  f"got L=0x{g_l[j]:08X} R=0x{g_r[j]:08X} "
                  f"want L=0x{e_l[j] & 0xFFFFFFFF:08X} R=0x{e_r[j] & 0xFFFFFFFF:08X}")
    print(f"\n=== STRICT 32-bit compare ===")
    print(f"words checked  : {strict_checked:,} x2 ch")
    print(f"mismatched fr  : {strict_bad:,}  rate="
          f"{strict_bad/max(1,strict_checked):.3e}")
    ok = (sc["defects"] == 0 and strict_bad == 0
          and scored >= 0.9 * got_n)
    print(f"\nDSP BIT-PERFECT ({a.rounding}): " + ("PASS" if ok else "FAIL"))
    if not ok:
        print("Reading a FAIL: the claim is compound -- the I2S loopback path "
              "AND this host port of adsp_biquad. Localise before blaming the "
              "device: a whole-stream mismatch with the correct delay is the "
              "port (try --rounding once); isolated mismatches at random "
              "positions are the path (one_shot_block.py at preset 0 is the "
              "control that separates them).")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
