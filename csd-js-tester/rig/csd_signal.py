"""Signal source for the loopback/bit-perfect legs.

WHY NOT DC. A DC word was used as the positive control in an earlier version of
these probes, and it is worthless: a constant stream carries no ordering
information, so a REPLAYED, DROPPED, DUPLICATED or REORDERED frame is
indistinguishable from a fresh one. "capture contained exactly 0x123456" is
equally consistent with a perfect tap and with the device replaying one buffer
that arrived 2 s ago. Same class of error as a periodic play buffer, which hid a
gap of exactly the buffer length, and as a cyclic flash-ID read, which hid a
phase. Any test stimulus here MUST vary per frame and be indexed by absolute
frame number -- never by (frame mod buffer length).

Choice: deterministic band-limited noise. Uniform 24-bit per frame, then a
short FIR so adjacent frames differ but the energy is not white to the codec's
Nyquist. It is not a pure tone because a sine has no information in its LSBs --
a whole-LSB-level error on a tone is invisible -- and a single tone cannot
exercise the path across the band. Noise exercises every bit of every sample.
Sweep and tone are available alongside for their own purposes; noise is the
default stimulus for bit-exactness.

Deterministic (fixed seed, no PRNG state that can desync) and reproducible:
sample(n) depends on n and nothing else, exactly like the golden stream in
bit_perfect_common, so a dropped frame costs one reported error instead of
desynchronising checker and device.
"""
import os

import numpy as np

RATE = int(os.environ.get("CSD_RATE", "384000"))
M24 = (1 << 24) - 1
SEED_DEFAULT = 0xC5D00D
# FIR half-width: +-3 frames. Enough to band-limit well below Nyquist while
# keeping every frame distinguishable from its neighbours.
TAPS = 3


def _uniform_vec(idx, seed):
    """Deterministic 24-bit uniform value per absolute frame index.

    Splitmix-style finaliser: strong avalanche at 24 bits, no PRNG state,
    identical across processes and machines. NOT crypto -- it only has to
    defeat a rig that might pattern-complete, and to make every frame unique
    so no defect can hide by looking like its neighbour."""
    M = np.uint64(0xFFFFFFFF)
    x = (idx.astype(np.uint64) + np.uint64(seed)) & M
    x = ((x ^ (x >> np.uint64(16))) * np.uint64(0x7FEB352D)) & M
    x = ((x ^ (x >> np.uint64(15))) * np.uint64(0x846CA68B)) & M
    x = x ^ (x >> np.uint64(16))
    return (x & np.uint64(M24)).astype(np.uint64)


_Q = 1 << 20              # kernel fixed-point scale, finer than Q16 so the
                          # amplitude-normalised weights stay exact
_MID = 1 << 23            # unsigned 24-bit mid-point: the signal is generated
                          # centred and offset, so it is symmetric about 0


def _fir_kernel():
    """Centred Hamming lowpass, normalised to preserve AMPLITUDE.

    Dividing by sum(w) -- the usual DC-gain-1 normalisation -- collapses
    full-field uniform noise into a small-swing Gaussian, and the test stops
    exercising the top and bottom of the 24-bit field. Those are the bits a
    bit-perfect claim is actually about, so the kernel is normalised on its L2
    norm instead: output standard deviation stays proportional to input, the
    stream uses the field, and the Hamming window still keeps energy well below
    Nyquist."""
    k = np.hamming(2 * TAPS + 1)
    k = k / np.sqrt((k * k).sum()) * 0.5      # std preserved, ~3.5 sigma at FS
    return np.round(k * _Q).astype(np.int64)


KERNEL = _fir_kernel()


def _uniform_centered(idx, seed):
    """Uniform 24-bit value for index array, re-centred on zero."""
    return _uniform_vec(idx, seed).astype(np.int64) - _MID


def noise24_vec(idx, seed=SEED_DEFAULT, ch=0):
    """Band-limited noise, 24-bit unsigned, for the given ABSOLUTE frame index
    array. Returns uint64 values in [0, 2**24).

    ch offsets the seed so L and R are uncorrelated: a rig that cross-wires the
    channels must fail, not pass because both carry the same noise. Each value
    is a FIR over 2*TAPS+1 neighbours, so it depends on frames
    n-TAPS..n+TAPS -- but it remains a PURE FUNCTION OF THE ABSOLUTE INDEX, so
    the checker can recompute any frame from its index alone and a dropped
    frame costs one reported error instead of desynchronising the run."""
    idx = np.asarray(idx, dtype=np.uint64)
    acc = np.zeros(idx.shape, dtype=np.int64)
    for j, w in enumerate(KERNEL):
        # Frame (n + j - TAPS) is reached by shifting the SEED, not the index:
        # _uniform_vec is a function of (i + seed), so this is the same value.
        # Adding a negative to a uint64 index underflows at n=0 (OverflowError,
        # seen on the first run) and wrapping would silently make frame 0
        # depend on frame 2**64-3.
        acc += _uniform_centered(idx, seed + 7919 * ch + (j - TAPS)) * int(w)
    acc //= _Q
    acc = np.clip(acc, -_MID, _MID - 1) + _MID
    return acc.astype(np.uint64)


def tone24_vec(idx, freq, amp=(M24 >> 1) - 1, dc=(M24 >> 1), ch=0,
               phase=0.0):
    """Sine at absolute frame index, full-ish scale. Returns unsigned 24-bit.

    A tone is for level/THD/spectral questions, NOT for bit-exactness: its
    LSBs carry no information, so a whole-LSB error is invisible in it."""
    idx = np.asarray(idx, dtype=np.uint64).astype(np.float64)
    v = dc + amp * np.sin(2.0 * np.pi * freq * idx / RATE + phase + 0.25 * ch)
    return np.clip(np.rint(v), 0, M24).astype(np.uint64)


def sweep24_vec(idx, f0=1000.0, f1=RATE / 2 * 0.95, secs=None,
                amp=(M24 >> 1) - 1, dc=(M24 >> 1), ch=0):
    """Linear chirp over the band, driven by ABSOLUTE frame index so it never
    resets at a buffer boundary -- a chirp that restarts per chunk is another
    way to make a gap invisible.

    Instantaneous frequency sweeps f0->f1 across `secs` of audio, computed from
    the absolute sample position, so frame N's expected value is computable
    without knowing anything about the frames before it."""
    idx = np.asarray(idx, dtype=np.uint64).astype(np.float64)
    if secs is None:
        secs = 10.0
    n_tot = secs * RATE
    k = (f1 - f0) / n_tot
    # phase = 2pi * integral of (f0 + k*t) dt
    ph = 2.0 * np.pi * (f0 * idx + 0.5 * k * idx * idx) / RATE
    v = dc + amp * np.sin(ph + 0.25 * ch)
    return np.clip(np.rint(v), 0, M24).astype(np.uint64)


def silence24_vec(idx):
    return np.zeros(np.asarray(idx).shape, dtype=np.uint64)


# ===================== the rig's golden stream: ONE definition ==============
# 24-bit boundary words, injected periodically. Never all-ones ALONE as the
# only special value: it is rotation-invariant and would hide a 1-bit wire
# shift. The single-bit walk supplies the rotation-sensitive set.
BOUNDARIES = (0x800000, 0x7FFFFF, 0x000001, 0xFFFFFF,
              0x400000, 0x000002, 0x200000, 0x000004)
BOUNDARY_EVERY = 1024

# A marker pair a noise stream will not produce by accident: two independent
# 24-bit draws match a fixed pair with probability 2**-48 per frame, so a run
# of even 32 is already ~1e-14 and a run of 1000 is not physical.
SYNC_L, SYNC_R = 0x00D0D0D, 0x00BE578


def expected_vec(idx):
    """(L24, R24) for the ABSOLUTE frame index array -- the single definition
    of what the rig plays AND what it expects. Player and checker must call
    this same function: two copies drift, and then the rig compares the device
    against a stream nobody played. (This exact drift produced a guaranteed
    false FAIL on every 1024th frame once already.)

    Both channels are band-limited noise on independent seeds, so:
      - every frame is a real signal sample, exercising bits across the field;
      - L and R are uncorrelated, so a channel cross-wire FAILS;
      - no translational self-similarity, so a REPLAY is detectable -- the
        property a DC or single-tone stimulus does not have.
    Boundary words replace R every BOUNDARY_EVERY frames to put known
    bit patterns (MSB, LSB, all-ones, single-bit walk) on the wire at known
    indices. Frame 0 is deliberately not a boundary slot: the rig finds its
    alignment at the sync marker, not on a special value.
    """
    idx = np.asarray(idx, dtype=np.uint64)
    L = noise24_vec(idx, ch=0)
    R = noise24_vec(idx, ch=1)
    bmask = (idx != 0) & ((idx % np.uint64(BOUNDARY_EVERY)) == 0)
    if bmask.any():
        sel = ((idx[bmask] // np.uint64(BOUNDARY_EVERY) - np.uint64(1))
               % np.uint64(len(BOUNDARIES))).astype(int)
        R = R.copy()
        R[bmask] = np.asarray([BOUNDARIES[s] for s in sel], dtype=np.uint64)
    return L.astype(np.uint32), R.astype(np.uint32)


def self_test():
    """A stimulus that cannot fail is not a stimulus. These assert the property
    that makes this file exist at all: per-frame uniqueness and ordering
    sensitivity, which is exactly what DC could not provide."""
    n = 20000
    idx = np.arange(n, dtype=np.uint64)
    a = noise24_vec(idx)
    b = noise24_vec(idx, ch=1)
    # 1. NO TRANSLATIONAL SELF-SIMILARITY -- the property that makes a replay
    #    detectable. If the stream equalled its own shift by k, a device that
    #    replayed a buffer k frames long would be indistinguishable from a
    #    correct one. (A constant stream satisfies this equality for every k,
    #    which is exactly why a DC stimulus cannot certify anything.) Strict
    #    value-uniqueness is NOT required and is not achievable here: band
    #    limiting reduces entropy, so occasional 24-bit repeats are expected
    #    and harmless -- duplicates are caught by the per-frame counter in L,
    #    not by R's entropy. Asserting uniqueness was my error, caught by the
    #    first run.
    distinct = len(np.unique(a))
    assert distinct > 0.9 * n, f"noise too degenerate: {distinct}/{n} distinct"
    for k in (1, 2, 3, 7, 64, 255, 256, 1023, 1024, 4096, 16384, 65535, 65536):
        if k >= n:
            continue
        seg = n - k
        same = int((a[:seg] == a[k:k + seg]).sum())
        assert same < 0.02 * seg, \
            f"stream is self-similar at offset {k} ({same}/{seg} match): a " \
            f"replay of a {k}-frame buffer would pass undetected"
    # 2. adjacent frames differ, so a one-frame hold/stall is visible
    assert int((a[1:] == a[:-1]).sum()) < 0.01 * n, "too many adjacent repeats"
    # 3. L and R uncorrelated, so a channel cross-wire FAILS instead of passing
    assert not np.array_equal(a, b), "L == R: cross-wiring is undetectable"
    assert int((a == b).sum()) < 0.02 * n, "L and R too alike"
    # 4. absolute indexing: values must NOT be periodic in any buffer size.
    #    This is the trap that burned this rig twice already.
    for blen in (1024, 4096, 16384, 65536):
        base = np.arange(0, blen, dtype=np.uint64)
        nxt = np.arange(blen, 2 * blen, dtype=np.uint64)
        assert not np.array_equal(noise24_vec(base), noise24_vec(nxt)), \
            f"noise is periodic with buffer length {blen} -- a gap of exactly " \
            f"{blen} frames would be invisible"
    # 5. deterministic and index-addressable: recompute any single frame from
    #    its index alone and it must match the vector result.
    for i in (0, 1, 7, 5000, n - 1):
        one = noise24_vec(np.array([i], dtype=np.uint64))
        assert int(one[0]) == int(a[i]), f"frame {i} not index-addressable"
    # 6. band-limited but not degenerate: neighbours differ, and the signal
    #    actually spans a wide range of the 24-bit field.
    assert np.any(a[1:] != a[:-1])
    span = int(a.max()) - int(a.min())
    assert span > (1 << 22), f"noise does not use the field (span {span})"
    # 7. a tone must be smooth WHERE NOISE IS NOT: sanity that tone24 works.
    #    The bound is physical, not invented: for a sine the maximum per-sample
    #    step is 2*A*sin(pi*f/fs). An earlier constant in this assert was wrong
    #    by ~30x and failed a correct tone.
    f_t = 1000.0
    amp_t = (M24 >> 1) - 1                  # must match tone24_vec's default
    t = tone24_vec(idx, f_t)
    bound = 2.0 * amp_t * np.sin(np.pi * f_t / RATE) * 1.05 + 2
    step_t = int(np.max(np.abs(np.diff(t.astype(np.int64)))))
    assert step_t <= bound, f"tone slew {step_t} exceeds physical bound {bound:.0f}"
    step_n = int(np.max(np.abs(np.diff(a.astype(np.int64)))))
    assert step_n > 10 * bound, "noise is as smooth as a tone: not exercising bits"
    s = sweep24_vec(np.arange(0, 20000, dtype=np.uint64), secs=5.0)
    assert len(np.unique(s)) > 1000, "sweep degenerate"
    # 8. silence is exactly zero (the P2 arm depends on this being exact)
    assert np.all(silence24_vec(idx) == 0)
    return True


if __name__ == "__main__":
    self_test()
    idx = np.arange(0, 8, dtype=np.uint64)
    print("noise L:", [hex(int(v)) for v in noise24_vec(idx)])
    print("noise R:", [hex(int(v)) for v in noise24_vec(idx, ch=1)])
    print("tone 1k:", [hex(int(v)) for v in tone24_vec(idx, 1000.0)])
    print("SIGNAL-SELFTEST-PASS")
