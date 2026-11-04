import sys
import numpy as np

sys.path.insert(0, "rig")
from dsp_bit_perfect import find_delta, describe_residual

rng = np.random.default_rng(7)
N, DTRUE = 2_000_000, 991

uL = rng.integers(-2**23, 2**23, N).astype(np.int32)
uR = rng.integers(-2**23, 2**23, N).astype(np.int32)

# Rig's convention (build_regions scores uL[idx + delta]): capture frame i
# carries schedule frame i + delta. A bounded residual is ADDED -- exactly the
# case a run-match cannot see, which is what this test exists to protect.
capL = (uL[DTRUE:] + rng.integers(-30, 31, N - DTRUE)).astype(np.int32)
capR = (uR[DTRUE:] + rng.integers(-30, 31, N - DTRUE)).astype(np.int32)
capL = np.concatenate([capL, np.zeros(DTRUE, np.int32)])
capR = np.concatenate([capR, np.zeros(DTRUE, np.int32)])

d, mass, spike, pairs = find_delta(capL, capR, uL, uR)
print(f"synthetic (bounded residual): true delta={DTRUE}  found={d}  "
      f"mass={100*mass:.2f}%  spike={spike:,}  pairs={pairs:,}")
assert d == DTRUE, f"WRONG DELTA (found {d}, true {DTRUE})"

# The threshold is NOT invented: it is the locator's own null distribution,
# measured by the shuffled control below. Mass dilutes with residual size --
# only samples whose noise is exactly 0 land on their schedule value -- so a
# fixed "50%" bar would fail a correct locator. What must hold is that the
# spike towers over the non-ordered baseline.
#
# BOTH channels must be shuffled: find_delta returns the better of the two, so
# breaking only L would still report the intact R spike and the control would
# "pass" while testing nothing.
shL, shR = uL.copy(), uR.copy()
rng.shuffle(shL)
rng.shuffle(shR)
d2, m2, _, p2 = find_delta(capL, capR, shL, shR)
print(f"control (shuffled schedule L+R): delta={d2} mass={100*m2:.4f}% "
      f"pairs={p2:,}")
assert mass > 20 * max(m2, 1e-6), f"SPIKE NOT DISTINCT FROM NULL ({mass} vs {m2})"
assert m2 < 0.002, f"CONTROL LEAKED ({m2})"
print(f"  PASS  spike {mass/max(m2,1e-9):,.0f}x the null, correct delta")

describe_residual(capL, capR, uL, uR, d)

d3, m3, s3, p3 = find_delta(uL.copy(), uR.copy(), uL, uR)
print(f"exact case (no residual): delta={d3} mass={100*m3:.2f}%")
assert d3 == 0 and m3 > 0.5, f"EXACT CASE FAILED (d={d3}, m={m3})"
print("  PASS")
print("ALL ANCHOR GATES PASS")
