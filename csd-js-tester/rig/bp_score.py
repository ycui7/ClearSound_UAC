"""Slip-tolerant, bit-level scorer for the loopback bit-perfect gate.

WHY THIS EXISTS. The gate's original walk counted only the frames that matched
BEFORE THE FIRST MISMATCH in each captured read, then re-synced by content and
never scored the rest of that read. With one framing slip per read (measured:
gap/dup == number of reads), that reports ~0.003% even for a stream whose every
sample is correct -- and it cannot tell a firmware bit error from a capture
bookkeeping slip. Both failure modes print the same near-zero number, which is
how a clean path looked broken for days.

WHAT THIS DOES INSTEAD. For each read it predicts the schedule offset from the
previous read, verifies that prediction against the head of the read, and only
searches for a new offset when the prediction fails. It then scores the WHOLE
read at the chosen offset -- every frame, and differing BITS rather than a
percentage of frames -- so a slip costs one re-sync, not the rest of the run.
Slips are reported separately from bit errors, because they are a property of
the capture path, not of the audio data.

Reported: frames checked, frames exactly equal, differing bits total, worst
bits per frame, slip count, zero-word frames, and the worst 1-second block so a
drift that grows over the run is visible.
"""
import numpy as np

HEAD = 512          # frames used to verify a predicted offset
SEARCH = 8192       # +/- frames searched when the prediction fails


def _split(chunk_bytes):
    f = np.frombuffer(chunk_bytes, dtype="<u4")
    a = (f[0::2] >> np.uint32(8)).astype(np.int64)
    b = (f[1::2] >> np.uint32(8)).astype(np.int64)
    return a, b


def score_chunks(gL, gR, chunks, rate, start=None, first=0, skip_head=0):
    n = len(gL)
    exact = checked = bits = 0
    slips = zero_frames = 0
    worst_block = (0.0, -1)
    block_len = max(1, int(rate))
    blk_ok = blk_n = 0
    pred = start
    per_frame_max = 0
    head_skipped = False

    for ci, (c, w, t) in enumerate(chunks):
        if ci < first:
            continue
        a, b = _split(c)
        m = len(a)
        if m == 0:
            continue

        off = None
        if pred is not None:
            h = min(HEAD, m)
            e = pred + h
            if e <= n and np.array_equal(a[:h], gL[pred:e]) and np.array_equal(b[:h], gR[pred:e]):
                off = pred
        if off is None:
            ref = min(HEAD, m)
            lo = max(0, (pred if pred is not None else 0) - SEARCH)
            hi = min(n - ref, (pred if pred is not None else 0) + SEARCH)
            best = None
            for d in range(lo, hi + 1):
                ok = int(np.count_nonzero((a[:ref] == gL[d:d + ref]) &
                                          (b[:ref] == gR[d:d + ref])))
                if best is None or ok > best[0]:
                    best = (ok, d)
                    if ok == ref:
                        break
            if best is None or best[0] == 0:
                continue
            off = best[1]
            if pred is not None:
                slips += 1

        e = min(off + m, n)
        if not head_skipped and skip_head:
            # The first frames after the stream joins are a start ramp (buffers
            # filling, first conversions) -- measured, not assumed: the caller
            # scores with and without this window and the two numbers have to
            # account for each other.
            off = min(off + int(skip_head), n)
            head_skipped = True
        L = e - off
        if L <= 0:
            continue
        xl = np.bitwise_xor(a[:L], gL[off:e])
        xr = np.bitwise_xor(b[:L], gR[off:e])
        good = (xl == 0) & (xr == 0)
        n_ok = int(good.sum())
        exact += n_ok
        checked += L
        zero_frames += int(((a[:L] == 0) & (b[:L] == 0)).sum())

        for x in (xl, xr):
            nz = x[x != 0]
            if nz.size:
                arr = nz.astype(np.uint64)
                try:
                    bits += int(np.bitwise_count(arr).sum())
                    per_frame_max = max(per_frame_max,
                                        int(np.bitwise_count(arr).max()))
                except AttributeError:
                    bits += sum(bin(int(v)).count("1") for v in nz.tolist())
                    per_frame_max = max(per_frame_max,
                                        max(bin(int(v)).count("1") for v in nz.tolist()))

        blk_ok += n_ok
        blk_n += L
        if blk_n >= block_len:
            frac = blk_ok / blk_n
            if frac < worst_block[0] or worst_block[1] < 0:
                worst_block = (frac, checked)
            blk_ok = blk_n = 0

        pred = e

    return dict(exact=exact, checked=checked, bits=bits, slips=slips,
                zero_frames=zero_frames, worst_block=worst_block,
                max_bits_per_frame=per_frame_max)
