#!/usr/bin/env python3
"""Rare-value delta histogram over all four L/R pairings. Decisive, one pass.

Method note (why an earlier run was void): np.unique returns
(values, FIRST_INDEX, counts). Using the second return as a count selects
"value first seen in the first N slots" and reports an empty set as a
measurement. Counts only, here.

Why this test and not a run-match: only ~5% of schedule frames hold a value
that occurs once, and requiring a long contiguous run can never succeed when
the true map has structure rather than being a pure offset. Instead: take
values that are rare on BOTH sides, locate them unambiguously, and histogram
schedule_pos - capture_pos. A real ordered stream produces one tall spike
carrying essentially all the mass. Chance produces a flat profile.

The four pairings matter because L and R of this rig's own schedule share 86.5%
of their value sets, so a single-channel test cannot tell "correct channel" from
"the other channel" -- only the delta spike's strength can.
"""
import numpy as np

cap = np.load("/tmp/dsp_cap.npy")
cL = cap[0].astype(np.uint32) & np.uint32(0xFFFFFF)
cR = cap[1].astype(np.uint32) & np.uint32(0xFFFFFF)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
fL = (exp[0].astype(np.uint32) >> np.uint32(8)) & np.uint32(0xFFFFFF)
fR = (exp[1].astype(np.uint32) >> np.uint32(8)) & np.uint32(0xFFFFFF)
del exp
print(f"capture {len(cL):,}  schedule {len(fL):,}")


def analyse(capv, sch, tag):
    uv, uf, cf = np.unique(sch, return_index=True, return_counts=True)
    uc, _, cc = np.unique(capv, return_index=True, return_counts=True)
    cand = uv[cf <= 3]                              # rare on the schedule side
    srt = np.sort(capv)
    ordv = np.argsort(capv, kind="stable")
    lo = np.searchsorted(srt, cand, "left")
    hi = np.searchsorted(srt, cand, "right")
    keep = (hi > lo) & ((hi - lo) <= 3)             # and rare on the capture side
    pos_c = ordv[lo[keep]].astype(np.int64)
    pos_f = uf[cf <= 3][keep].astype(np.int64)
    n = len(pos_c)
    if n < 500:
        print(f"  {tag}: only {n} rare-on-both pairs -- VOID, not negative")
        return
    d = pos_f - pos_c
    v, c = np.unique(d, return_counts=True)
    o = np.argsort(-c)[:4]
    modal, modal_n = int(v[o[0]]), int(c[o[0]])
    print(f"  {tag}: {n:,} pairs | modal delta {modal:,} carries "
          f"{modal_n:,} ({100.0*modal_n/n:.2f}%) | next "
          + ", ".join(f"{int(v[t]):,}x{int(c[t])}" for t in o[1:]))
    if modal_n / n > 0.5:
        print(f"      ==> ORDERED COPY at delta {modal:,}")
    return modal, modal_n / n


print("\nmodal delta per pairing (a real ordered path shows ONE delta holding "
      "most of the mass):")
analyse(cL, fL, "capL vs sched L")
analyse(cR, fR, "capR vs sched R")
analyse(cL, fR, "capL vs sched R  (crosstalk)")
analyse(cR, fL, "capR vs sched L  (crosstalk)")

# The control that should never have been skipped: the same test against the
# UNFILTERED block. Membership percentages cannot separate filtered from
# unfiltered (the two sets overlap heavily -- same generator, one biquad
# stage), but a delta spike can: only the schedule the device actually sent
# has its values at the right positions. If the UNFILTERED pair spikes and the
# filtered one does not, the DSP never ran and every membership number in this
# session was set-level coincidence.
import sys
sys.path.insert(0, ".")
import one_shot_block as O
rL, rR, _ = O.build_block(len(fL))
rL = rL.astype(np.uint32) & np.uint32(0xFFFFFF)
rR = rR.astype(np.uint32) & np.uint32(0xFFFFFF)
print("\nCONTROL -- same test against the unfiltered block:")
analyse(cL, rL, "capL vs UNFILTERED L")
analyse(cR, rR, "capR vs UNFILTERED R")
