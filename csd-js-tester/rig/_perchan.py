import numpy as np, sys
sys.path.insert(0, ".")

WIN = 8192
NANCHOR = 300

cap = np.load("/tmp/dsp_cap.npy")
cL, cR = cap[0].astype(np.uint32), cap[1].astype(np.uint32)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
fL = (exp[0].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
fR = (exp[1].astype(np.uint32) >> np.uint32(8)).astype(np.uint32)
del exp

# The capture is the marker-step-response ramp at its head, so mid-stream.
off = 6 << 20
segL, segR = cL[off:off + WIN], cR[off:off + WIN]
print(f"window kept@{off:,}, {len(segL)} frames")

srtL = np.sort(fL); ordL = np.argsort(fL, kind="stable")
srtR = np.sort(fR); ordR = np.argsort(fR, kind="stable")


def pos(srt, ordv, v, capn=200):
    lo = np.searchsorted(srt, v, "left"); hi = np.searchsorted(srt, v, "right")
    if hi - lo > capn:
        return np.array([], dtype=np.int64)
    return ordv[lo:hi]


ds = []
for a in range(NANCHOR):
    for x in pos(srtL, ordL, int(segL[a])):
        ds.append(int(x) - (off + a))
ds = sorted(set(ds))
print(f"{len(ds)} candidate deltas from L anchors")

bestL = (-1, 0); bestR = (-1, 0); bestB = (-1, 0)
for d in ds[:400]:
    lo, hi = max(0, -d), min(len(segL), len(fL) - d)
    if hi - lo < 1024:
        continue
    i = np.arange(lo, hi, dtype=np.int64)
    ml = int(np.count_nonzero(segL[i] == fL[i + d]))
    mr = int(np.count_nonzero(segR[i] == fR[i + d]))
    mb = int(np.count_nonzero((segL[i] == fL[i + d]) & (segR[i] == fR[i + d])))
    bestL = max(bestL, (ml, d)); bestR = max(bestR, (mr, d))
    bestB = max(bestB, (mb, d))

for tag, (m, d) in (("L-only ", bestL), ("R-only ", bestR), ("L AND R", bestB)):
    print(f"  {tag}: {m:,}/{WIN:,} = {100.0*m/WIN:6.2f}% at delta={d:,}")

if bestL[0] >= 0.99 * WIN and bestR[0] < 0.5 * WIN:
    print("\n>>> L is bit-exact, R is not: the defect is on the R leg only.")
elif bestL[0] < 0.5 * WIN and bestR[0] >= 0.99 * WIN:
    print("\n>>> R is bit-exact, L is not: the defect is on the L leg only.")
elif bestL[0] >= 0.99 * WIN and bestR[0] >= 0.99 * WIN:
    print(f"\n>>> BOTH channels bit-exact but never together: PAIRING/phase "
          f"defect (dL={bestL[1]:,}, dR={bestR[1]:,}, skew={bestR[1]-bestL[1]:+,}).")
else:
    print("\n>>> Neither channel aligns as a run. Membership is set-level only.")
    # how good is a single frame at the best L candidate? (1-of-N sanity)
    print("    checking whether ANY single frame matches at all...")
    d = bestL[1]
    i = np.arange(0, 256, dtype=np.int64)
    hits = int(np.count_nonzero(segL[i] == fL[i + d]))
    print(f"    first 256 frames at d={d:,}: {hits} match")
