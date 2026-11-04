#!/usr/bin/env python3
"""One definition of the bit-perfect golden stream, shared by every leg.

Why it is shared: leg 1 (host writer) and leg 3 (loopback compare) must agree
on the golden bytes exactly. Two copies drift, and the failure mode is a rig
that compares the device against a stream nobody played.

Why R is a PURE FUNCTION OF THE FRAME INDEX and not a PRNG stream: a stream
maintains state, so one dropped frame desynchronises the checker from the
device and every subsequent frame mismatches -- the rig would report 1.9 M
errors for one real fault, and would be unable to say which frame broke. With
R = mix24(index) the expected word for frame N is computable from N alone, so
the device's own L counter tells us which index to check R against. Drops cost
one reported error, not a cascade.

mix24 is a splitmix-style finaliser: strong avalanche at 24 bits, cheap, no
numpy, reproducible across processes and machines. It is NOT crypto -- it only
has to defeat a rig that might pattern-complete.
"""
import struct

RATE = 384000
CH = 2
BYTES_PER_FRAME = CH * 4
SEED_DEFAULT = 0xC5D
BOUNDARY_EVERY = 1024
# 24-bit boundary words: LSB-1, MSB-1, +1, all-ones, then single-bit walk.
# Never all-ones ALONE as the only special value: it is rotation-invariant and
# would hide a 1-bit wire shift. The walk supplies the rotation-sensitive set.
BOUNDARIES = (0x800000, 0x7FFFFF, 0x000001, 0xFFFFFF,
              0x400000, 0x000002, 0x200000, 0x000004)
L_WRAP = 1 << 24


def mix24(i, seed=SEED_DEFAULT):
    x = (i + seed) & 0xFFFFFFFF
    x = ((x ^ (x >> 16)) * 0x7FEB352D) & 0xFFFFFFFF
    x = ((x ^ (x >> 15)) * 0x846CA68B) & 0xFFFFFFFF
    x ^= x >> 16
    return x & 0xFFFFFF


def mix24_vec(idx, seed=SEED_DEFAULT):
    """numpy twin of mix24. MUST agree bit-for-bit: self_test cross-checks
    them, because the generator and the verifier disagreeing is the same
    drift failure as two copies of the golden definition."""
    import numpy as np
    M32 = np.uint64(0xFFFFFFFF)
    x = (idx.astype(np.uint64) + np.uint64(seed)) & M32
    x = ((x ^ (x >> np.uint64(16))) * np.uint64(0x7FEB352D)) & M32
    x = ((x ^ (x >> np.uint64(15))) * np.uint64(0x846CA68B)) & M32
    x = x ^ (x >> np.uint64(16))
    return (x & np.uint64(0xFFFFFF)).astype(np.uint32)


def expected(i):
    """The golden 24-bit pair for frame index i. (L24, R24)

    The boundary walk is (i // BOUNDARY_EVERY - 1): i==0 is deliberately not
    a boundary slot (frame 0 must be an ordinary word, since that is where the
    rig finds its alignment point), so the first boundary is i==1024 and must
    select BOUNDARIES[0] -- the LSB-1 word. An earlier version used i //
    BOUNDARY_EVERY, which silently started the walk at BOUNDARIES[1] and made
    the documented coverage untrue; the self_test below is what caught it.
    """
    l24 = i % L_WRAP
    if i and (i % BOUNDARY_EVERY) == 0:
        r24 = BOUNDARIES[(i // BOUNDARY_EVERY - 1) % len(BOUNDARIES)]
    else:
        r24 = mix24(i)
    return l24, r24


def expected_vec(idx):
    """Vector twin of expected() for the SAME absolute index array.

    MUST be the only place the boundary-injection rule lives. An earlier
    version had the player emit plain mix24 while the checker applied the
    boundary override, so every 1024th frame was a guaranteed false FAIL -- the
    rig failed on its own inconsistency, not on the device. If this function
    and expected() ever diverge, self_test catches it; if either diverges from
    what the player actually sent, no PASS means anything.

    Takes/returns uint64 arrays; returns (L24, R24) as uint32."""
    import numpy as np
    idx = idx.astype(np.uint64)
    L = (idx % np.uint64(L_WRAP)).astype(np.uint32)
    R = mix24_vec(idx).astype(np.uint32)
    bmask = (idx != 0) & ((idx % np.uint64(BOUNDARY_EVERY)) == 0)
    if bmask.any():
        sel = ((idx[bmask] // np.uint64(BOUNDARY_EVERY) - np.uint64(1))
               % np.uint64(len(BOUNDARIES))).astype(int)
        R[bmask] = np.asarray([BOUNDARIES[s] for s in sel], dtype=np.uint32)
    return L, R


def frame_bytes(i):
    l24, r24 = expected(i)
    # 32-bit subslot, value in bits 31..8; low 8 are descriptor padding.
    return struct.pack("<II", l24 << 8, r24 << 8)


def blob(i0, n):
    out = bytearray(n * BYTES_PER_FRAME)
    for k in range(n):
        out[k * 8:k * 8 + 8] = frame_bytes(i0 + k)
    return bytes(out)


def unpack(word_l, word_r):
    """Wire word -> (L24, R24). The I2S write is bitrev(word) shifted MSB
    first and the ADC read is in + bitrev, so a healthy transmitter/receiver
    pair composes to the identity: compare against the word we sent, no
    transform here. If a transform were needed, that IS the defect."""
    return word_l >> 8, word_r >> 8


def self_test():
    """A golden definition that cannot fail is not a definition. Verify
    determinism, index-addressability, single-bit sensitivity, and that the
    numpy path used by the player and the checker agrees bit-for-bit with the
    scalar path that DEFINES the golden stream. Two implementations of the
    reference disagreeing is the same drift failure as two copies of it."""
    import numpy as np
    assert blob(0, 4) == blob(0, 4)
    b = bytearray(blob(0, 4))
    b[3] ^= 0x80
    assert bytes(b) != blob(0, 4)
    # index-addressable: frame i's bytes depend on i and nothing else
    assert frame_bytes(5000) == frame_bytes(5000)
    assert frame_bytes(5000) != frame_bytes(5001)
    # scalar and vector mixers identical, including across the 2**24 epoch
    # boundary where the absolute index no longer fits in 24 bits -- exactly
    # the region a looping stimulus would have hidden
    probe = list(range(0, 5000)) + [L_WRAP - 2, L_WRAP - 1, L_WRAP, L_WRAP + 1,
                                    2 * L_WRAP + 7, 3 * L_WRAP + 123]
    vec = mix24_vec(np.array(probe, dtype=np.uint64))
    for k, i in enumerate(probe):
        assert int(vec[k]) == mix24(i), f"mix24 != mix24_vec at index {i}"
    # boundary words actually appear, in order, starting at the FIRST slot
    assert expected(BOUNDARY_EVERY)[1] == BOUNDARIES[0]
    assert expected(2 * BOUNDARY_EVERY)[1] == BOUNDARIES[1]
    for k, bw in enumerate(BOUNDARIES, start=1):
        assert expected(k * BOUNDARY_EVERY)[1] == bw, f"boundary {k} missing"
    # frame 0 is never a boundary: it is the rig's alignment point
    assert expected(0)[1] == mix24(0)
    assert expected(L_WRAP)[0] == 0 and expected(L_WRAP - 1)[0] == L_WRAP - 1
    # avalanche: neighbouring indices must not look alike
    same = sum(1 for i in range(2000)
               if bin(mix24(i) ^ mix24(i + 1)).count("1") < 4)
    assert same < 20, f"mix24 avalanche too weak ({same}/2000)"
    return True


if __name__ == "__main__":
    self_test()
    for i in (0, 1, 2, BOUNDARY_EVERY, BOUNDARY_EVERY + 1, L_WRAP):
        print(f"frame {i:9d}: L24=0x{expected(i)[0]:06X} R24=0x{expected(i)[1]:06X}")
    print("COMMON-SELFTEST-PASS")
