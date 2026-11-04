import numpy as np, sys

OFF = 6 << 20
WIN = 8192
REACH = 4000            # how far a repeat/drop resampler can move a sample

cap = np.load("/tmp/dsp_cap.npy")
cL, cR = cap[0].astype(np.uint32), cap[1].astype(np.uint32)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
fL = (exp[0].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
del exp

seg = cL[OFF:OFF + WIN]
# consecutive duplicates: a path that HOLDS samples produces equal neighbours,
# which band-limited noise essentially never does.
dups = int(np.count_nonzero(seg[1:] == seg[:-1]))
print(f"consecutive-equal frames in capture window: {dups:,}/{WIN:,}")

srt = np.sort(fL); order = np.argsort(fL, kind="stable")
resid = []
found = 0
for a in range(WIN):
    v = int(seg[a])
    lo = np.searchsorted(srt, v, "left"); hi = np.searchsorted(srt, v, "right")
    if hi == lo:
        continue
    ps = order[lo:hi]
    if len(ps) > 400:
        continue
    near = ps[np.abs(ps.astype(np.int64) - (OFF + a)) <= REACH]
    if len(near):
        found += 1
        resid.append(int(near[np.argmin(np.abs(near - (OFF + a)))]) - (OFF + a))

resid = np.array(resid)
print(f"frames whose value exists in the twin within +/-{REACH}: {found:,}/{WIN:,} "
      f"({100.0*found/WIN:.1f}%)")
if len(resid):
    print(f"  residual (schedule_pos - capture_pos): min {resid.min():,} "
          f"median {int(np.median(resid)):,} max {resid.max():,}")
    v, c = np.unique(resid, return_counts=True)
    o = np.argsort(-c)[:8]
    print("  modal residuals:", ", ".join(f"{int(v[t]):,}x{int(c[t])}" for t in o))
    print("\nREAD: high found% + residuals clustered on one value that DRIFTS")
    print("      => the stream is re-timed by a repeat/drop resampler: values")
    print("      are ours, the sample clock is not, so word-exactness across")
    print("      the loopback is not achievable and the gate belongs upstream")
    print("      of the ADC leg (preset 0 already proves that leg).")
elif found < 0.5 * WIN:
    print("\nREAD: values are not the twin's at any nearby position: the 94%")
    print("      membership was set-level coincidence, not identity.")
