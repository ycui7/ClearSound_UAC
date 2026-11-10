#!/usr/bin/env python3
"""Behaviour test for the upsampling polyphase bank's index identity.

csd_ups_block.c computes, per input block of frame_advance N and ratio R:
    q[(j << log2(R)) + p] = conv_k(x, h[k::R])[j]
and the wire pops q in index order. Asserted here against the ground truth:
a by-R upsampled signal is the FULL prototype convolved with the zero-stuffed
input, read at wire index. Also asserted: the per-phase fd filter's valid
output window (td_block_length + 1 - taps_per_block) covers frame_advance,
using the geometry the generator emitted into the conf header.

Run:  tools/dsp/.venv-dspgen/bin/python tools/dsp/test_ups_block.py
"""
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gen_ups_block as g


def check_ratio(R):
    N = g.FRAME_ADVANCE
    proto = np.array(g.design_prototype(R))
    assert abs(proto.sum() - R) < 0.05, f"R={R}: prototype sum {proto.sum()}"
    rng = np.random.default_rng(7)
    x = rng.uniform(-1.0, 1.0, N)

    # Ground truth: zero-stuff then convolve with the full prototype. The
    # prototype delay centres are dropped identically on both sides (we
    # compare a window away from the edges).
    xs = np.zeros(N * R)
    xs[::R] = x
    y_ref = np.convolve(xs, proto)

    # Engine form: per-phase convolution, de-interleaved by the push rule.
    q = np.zeros(N * R)
    for k in range(R):
        hk = proto[k::R]
        assert abs(hk.sum() - 1.0) < 0.05, f"R={R} phase {k}: sum {hk.sum()}"
        pk = np.convolve(x, hk)
        for j in range(N):
            q[(j << int(R).bit_length() - 1) + k] = pk[j]

    lo, hi = 4 * R, N * R - proto.size
    err = np.max(np.abs(q[lo:hi] - y_ref[lo:hi]))
    assert err < 1e-12, f"R={R}: de-interleave mismatch, max |err| {err}"
    print(f"R={R}: polyphase de-interleave matches zero-stuff conv "
          f"(max |err| {err:.2e} over {hi - lo} wire words)")


def check_conf_geometry():
    """The firmware refuses a bank whose window does not cover frame_advance;
    the shipped conf must not trip that refusal."""
    conf = open(os.path.join(g.OUT_DIR, "csd_ups_block_conf.h")).read()
    rows = re.findall(r"\{ coef_blocks_csdups_r(\d+)p(\d+), "
                      r"(\d+), (\d+), (\d+), (\d+), (\d+),", conf)
    assert rows, "conf has no phase rows"
    seen = set()
    for R, k, tdl, blocks, tpb, fa, fo in rows:
        R, tdl, tpb, fa = int(R), int(tdl), int(tpb), int(fa)
        assert int(k) < R
        assert fa == g.FRAME_ADVANCE, f"fa {fa}"
        assert tdl + 1 - tpb >= fa, f"r{R}p{k}: window too small"
        seen.add(R)
    assert seen == {2, 4, 8}, f"banks present: {seen}"
    print(f"conf: {len(rows)} phase rows, all windows cover frame_advance")


if __name__ == "__main__":
    for R in (2, 4, 8):
        check_ratio(R)
    check_conf_geometry()
    print("PASS")
