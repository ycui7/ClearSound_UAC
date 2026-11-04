#!/usr/bin/env python3
"""Is the capture the TX tap, or a real ADC sample? Three independent tests.

The tap site decides what this test can even mean:
  csd_mb_loop_put_site(samples_ctln_to_host[], 2) at xua_audiohub.xc:459
  is on the HOST-IN path (ADC -> host), not the TX path. But a TX-tap value
  is a word this rig generated; an ADC sample is a digitisation of whatever
  the analog input is actually hearing. Three signatures separate them:

  1. low byte: the rig's wire format parks the value in bits 31..8, so a
     TX-side copy always has byte0 == 0. The capture's byte0 is random ->
     the capture is NOT a TX tap. (Already measured: 14.7% zero, not 100%.)
  2. spectral: the stimulus is flat broadband; the twin carries the -4.5 dB
     shelf; an analog input hearing something else carries neither.
     Measure ZERO-FRAMES-EXCLUDED, or the 14% gate smears the answer.
  3. coincidence: with 23M values in a 2^24 space, the expected chance
     membership rate is ~1-exp(-23M/16.7M) = 74.8%. Anything near that is
     noise, not identity.
"""
import numpy as np
import sys
sys.path.insert(0, ".")
import one_shot_block as O

FS = 384000.0
cap = np.load("/tmp/dsp_cap.npy")
cL, cR = cap[0].astype(np.uint32), cap[1].astype(np.uint32)
exp = np.load("/tmp/dsp_expect_384000_vpu_23068672.npy")
eL32 = exp[0].astype(np.uint32)                     # filtered wire words

real = ~((cL == 0) & (cR == 0))
capL = cL[real]
capR = cR[real]

# ---- 1. byte0 signature ----------------------------------------------------
b0 = (capL & np.uint32(0xFF))
frac0 = float(np.mean(b0 == 0))
print(f"[1] byte0==0 in {frac0*100:.2f}% of kept capture L "
      f"(TX tap would be 100.00%, random wire value ~0.39%)")

# ---- 3. coincidence baseline ------------------------------------------------
rng = np.random.default_rng(11)
pick = rng.choice(len(capL), size=20000, replace=False)
samp = capL[pick]
uni = np.unique(eL32 >> np.uint32(8))
hit = int(np.count_nonzero(np.isin(samp, uni)))
uniq_frac = len(uni) / (1 << 24)
exp_hit = 1 - (1 - uniq_frac) ** 20000
print(f"[3] {hit}/20000 sampled capture values exist in the filtered "
      f"schedule; chance level for {len(uni):,} distinct values is "
      f"~{int(round(exp_hit))} ({100*uniq_frac:.1f}% coverage)")

# ---- 2. spectrum of the kept frames only -----------------------------------
def to24(a):
    u = (a.astype(np.uint32) >> np.uint32(8)) & np.uint32(0xFFFFFF)
    return np.where(u >= (1 << 23), u.astype(np.int64) - (1 << 24),
                    u.astype(np.int64))

def band_db(x, edges):
    N = 1 << 15
    n_seg = len(x) // N
    w = np.hanning(N).astype(np.float64)
    acc = np.zeros(N // 2 + 1)
    for s in range(n_seg):
        acc += np.abs(np.fft.rfft(to24(x[s * N:(s + 1) * N]).astype(np.float64) * w)) ** 2
    f = np.fft.rfftfreq(N, 1.0 / FS)
    p = np.array([acc[(f >= lo) & (f < hi)].mean()
                  for lo, hi in zip(edges[:-1], edges[1:])])
    ref = p[:3].mean()
    return 10 * np.log10(p / ref), p

edges = np.array([20, 45, 70, 90, 120, 180, 300, 600, 1200, 3000, 8000, 20000], float)
# the stimulus as the host generated it (unfiltered, no holes)
rL, rR, _ = O.build_block(1 << 24)
rL = rL.astype(np.uint32)
eLkept = (eL32 >> np.uint32(8)).astype(np.uint32)

for tag, arr in (("kept capture L", capL),
                 ("unfiltered stimulus", rL),
                 ("filtered twin (no holes)", eLkept[:len(capL)])):
    db, p = band_db(arr[:1 << 23], edges)
    treble = float(np.mean(db[3:]))
    print(f"\n[2] {tag}: treble(>90Hz) vs bass = {treble:+.2f} dB")
    print("    bands:", " ".join(f"{lo:.0f}-{hi:.0f}:{d:+.1f}"
                                 for (lo, hi), d in
                                 zip(zip(edges[:-1], edges[1:]), db)))
print("\nread: 0 dB -> DSP never ran (byte-exact bypass); -4.5 dB -> shelf is")
print("live; anything else -> the ADC is not hearing the DAC's I2S data.")
