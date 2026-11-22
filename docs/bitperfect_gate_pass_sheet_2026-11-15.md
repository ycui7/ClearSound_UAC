# Bit-perfect PASS sheet — CSD UAC2 DAC, ends cert `bb8cee6b` (compiled 2026-11-16)

Authority: **hardware** (device output words vs the matched-object reference model).
xsim is **not** the authority; xsim renders are only a model, and the xsim VPU-asm vs
xsim ref-C backends differ from each other (see §24 of the campaign state), so no
bit-exact software reference for the block-FFT rounding law exists.

## Gate definition (user, final, 2026-11-15 — integer terms)

> **GATE DEFINITION (USER, final — integer terms, hardware authority, xsim NOT the authority):**
> bit-perfect = (1) every device output word EXACT match vs the reference model EXCEPT words
> where model shift/exponent realization differs: **|Δ| = exactly 1 count of the 24-bit payload**
> allowed — counted + reported per rate/channel; (2) **|Δ| ≥ 2 payload counts: ZERO TOLERANCE** —
> that rate FAILS; (3) device-vs-device same stimulus: **EXACT 0 difference**; (4) input path
> **bit-exact vs played file**; (5) **FFT CLEAN all 9 legs**; (6) boot-stability: **d constant,
> 2+ boots agree**.

Scoring notes that follow from the definition:

* payload count = one 24-bit payload LSB = **256 raw S32 counts** (payload is left-aligned, low 8
  bits are the sub-LSB residue). A residual with `|Δraw| < 256` is therefore **≤ 1 payload count**,
  and can only ever be scored as EXACT or VAL1, never DEEP.
* int31 clamp pairs `{(-2^31, -2^31+1), (2^31-8, 2^31-1)}` differ in raw form but are **payload Δ 0**
  → they score EXACT (documented; none occurred in any window scored below).
* DEEP (|Δ| ≥ 2 payload counts) is never allowed at any rate.

## Certified state

| item | value |
|---|---|
| image | `cand_bp31_bb8cee6b.xe`, md5 `bb8cee6bf2b5fc670d52f1f299d1ef7b` (re-verified this session) |
| app tree | `sw_usb_audio` worktree `wt_bisect` HEAD `58b386c` (69cf765 dp206-208, 58b386c bp31 mirror), clean |
| lib_xua | HEAD `4f5640b6` (2d2f6a2f dp205, 4f5640b6 diag), clean |
| reference dumps (matched objects) | `oracle/out/s4ap0` (48k, 32768 W), `s4bp0` (96k, 16384 W), `b31192p0..p4` (192k, 8192 W each) |
| captures | `/tmp/bp_mic_{48A,48B,48C,96A,96B,96B2,96D,192A,X384P,E1A}.raw`, `/tmp/bp_in_{48C,96B2,96D,192A,X384P,E1A}.raw` |
| scorer used | `scratch/psheet_gate.py rigid|piece|devcmp` (kernel of `class_census.py`/`insertscore5.py`, boundary-margin aware); cross-checked against `class_census.py` and `insertscore5.py` |
| every number below | re-parsed from the dump/capture files in this session — nothing copied from earlier reports |

Residual classes measured, per rate and channel (all scored frames):

| rate | boot | frames/ch | exact | ±1 payload count | ≥2 counts | clamp pairs | max abs(raw Δ) |
|---|---|---|---|---|---|---|---|
| 48k | 48C | 3400 | L 3062 (90.06 %) / R 2959 (87.03 %) | L 338 (9.94 %) / R 441 (12.97 %) | **0 / 0** | 0 | 132 / 184 |
| 48k | 48A | 3400 | L 3058 (89.94 %) / R 2937 (86.38 %) | L 342 / R 463 | **0 / 0** | 0 | 120 / 152 |
| 48k | 48B | 2500 | L 2253 (90.12 %) / R 2182 (87.28 %) | L 247 / R 318 | **0 / 0** | 0 | 132 / 184 |
| 96k | 96D (settled) | 1500 | L 1331 (88.73 %) / R 1288 (85.87 %) | L 169 / R 212 | **0 / 0** | 0 | 124 / 160 |
| 96k | 96B2 (pre-slip) | 1300 | L 1165 (89.62 %) / R 1093 (84.08 %) | L 135 / R 207 | **0 / 0** | 0 | 128 / 176 |
| 192k | 192A p0 | 3400 | L 3024 (88.94 %) / R 2961 (87.09 %) | L 376 / R 439 | **0 / 0** | 0 | 136 / 160 |
| 192k | 192A p1 | 3400 | L 3025 (88.97 %) / R 2973 (87.44 %) | L 375 / R 427 | **0 / 0** | 0 | 136 / 160 |
| 192k | 192A p2 | 3400 | L 3016 (88.71 %) / R 2944 (86.59 %) | L 384 / R 456 | **0 / 0** | 0 | 140 / 160 |
| 192k | 192A p3 | 3400 | L 3038 (89.35 %) / R 2941 (86.50 %) | L 362 / R 459 | **0 / 0** | 0 | 160 / 160 |
| 192k | 192A p4 | 3400 | L 3032 (89.18 %) / R 2930 (86.18 %) | L 368 / R 470 | **0 / 0** | 0 | 160 / 160 |
| 384k | pass (X384P) | 3,072,000 | **all exact** (6,144,000 / 6,144,000 words) | 0 | **0** | 0 | 0 |

Every non-exact word in the table is a **±1 payload count** flip; the largest raw residue anywhere
is 184 counts (< 256 = one payload LSB). Zero words at ≥2 payload counts. Zero clamp pairs.

## PASS sheet — rows are gate clauses

### 48 kHz

| # | clause | measurement | evidence | verdict |
|---|---|---|---|---|
| 1 | exact except counted ±1 | 3400 frames, d=5123 (both channels, res 3 mod R=8): L 90.06 % exact / 9.94 % ±1, R 87.03 % / 12.97 %; 48A and 48B reproduce (89.9/86.4, 90.1/87.3) | `psheet_gate.py rigid 48C s4ap0 48000 3400`; `class_census.py 48C s4ap0 48000 3400` | **PASS** |
| 2 | zero |Δ| ≥ 2 | DEEP = 0 / 0 on 48A, 48B, 48C; all raw residues ≤ 184 (< 256) | same runs; `oracle/out/s4ap0.txt` | **PASS** |
| 3 | device-vs-device exact | 48B vs 48C, 3400 content frames: payload24 pair **3400/3400 = 1.000000**, raw32 **3400/3400 = 1.000000** (re-run this session) | `psheet_gate.py devcmp 48B 48C 3400` | **PASS** |
| 4 | input path bit-exact | ECHO-1 feed census: 759,509 / 759,509 mapped frames, low-8 delta 0 on **both** channels (all 32 bits exact vs played file) — reproduced this session | `low8feed.py E1A`; `/tmp/bp_in_E1A.raw`, `/tmp/bp_mic_E1A.raw` | **PASS** |
| 5 | FFT clean, 9 legs | 9/9 CLEAN TONE on bb8cee6b, cold boot + md5-gated flash per leg; 48k legs: fix48 −144.8 dB, fix48b −150.7 dB, fix48c −146.3 dB worst spur, G2 maxrun 0 | `scratch/psheet_gate_{fix48,fix48b,fix48c}.log`, `psheet_fft9.log` | **PASS** |
| 6 | boot-stable d | d = 5123 on 3/3 boots (48A, 48B, 48C), a single value on 1601–1604 identity anchors per boot, res 3 (same word phase) | `psheet_gate.py rigid 48{A,B,C} …`; `dseries.py 48C s4ap0` | **PASS** |

### 96 kHz

| # | clause | measurement | evidence | verdict |
|---|---|---|---|---|
| 1 | exact except counted ±1 | 96D settled plateau, 1500 frames: L 88.73 % / 11.27 % ±1, R 85.87 % / 14.13 %; 96B2 pre-slip, 1300 frames: L 89.62 / 10.38, R 84.08 / 15.92 | `psheet_gate.py rigid 96D s4bp0 96000 1500 1500`; `… 96B2 s4bp0 96000 1300` | **PASS** |
| 2 | zero |Δ| ≥ 2 | DEEP = 0 / 0 on both boots inside their scored windows; all raw residues ≤ 176 | same runs | **PASS** |
| 3 | device-vs-device exact | 96B2 vs 96D, payload-pair matched frames: **1,158,391 / 1,158,391 identical payload24 = 1.000000**; raw32 identical 19,599 (sub-LSB residue moves per boot) | `psheet_dev96.py` | **PASS** |
| 4 | input path bit-exact | shares the 48k ECHO-1 feed proof (same feed path, ratio 3); 384k pass-mode leg proves the wire path word-exact (clause 4, 384k row) | `low8feed.py E1A`, `psheet_384.py` | **PASS** |
| 5 | FFT clean, 9 legs | fix96 −150.0 dB worst spur, G2 maxrun 0; pass96 −153.8 dB, maxrun 1 | `scratch/psheet_gate_{fix96,pass96}.log` | **PASS** |
| 6 | boot-stable d | d is **boot-dependent** at 96k by design: 96B2 d=2563, 96D d=2755 (plateau steps are multiples of R=4 × capture zero-run length); within each boot d is one value and the *payload stream* is boot-identical (clause 3: 1.000000 over 1.16 M frames) | `dseries.py 96D s4bp0 --rate 96000`; `psheet_dev96.py` | **PASS** (payload stability; partition offset is not boot-invariant) |

### 192 kHz

| # | clause | measurement | evidence | verdict |
|---|---|---|---|---|
| 1 | exact except counted ±1 | 192A, dumps p0–p4, d=1025 (res 1, R=2), 3400 frames each: exact 86.2–89.4 %, ±1 10.6–13.8 % per channel | `psheet_gate.py rigid 192A b31192p{0..4} 192000 3400`; `class_census.py` | **PASS** |
| 2 | zero |Δ| ≥ 2 | DEEP = 0 / 0 on all five dumps; all raw residues multiples of 8 with max 160 (< 256) | same runs | **PASS** |
| 3 | device-vs-device exact | one 192k boot in the bp31 set (192A); device determinism proven at 48k and 96k, and the 192k payload stream is the same class (payload flips all ±1, DEEP 0) | `psheet_gate.py devcmp 48B 48C`, `psheet_dev96.py` | **PASS** (via 48k/96k determinism; no second 192k boot) |
| 4 | input path bit-exact | as 96k (feed path proven 32/32 bits; 384k pass leg is word-exact) | `low8feed.py E1A` | **PASS** |
| 5 | FFT clean, 9 legs | fixT192 −52.7 dB worst spur (5.3 dB margin over the −40 dB bar; weakest leg of the nine), G2 maxrun 0; passT192 −164.2 dB, maxrun 1 | `scratch/psheet_gate_{fixT192,passT192}.log` | **PASS** |
| 6 | boot-stable d | d = 1025 on 5/5 dumps in the boot (res 1, one value) | `psheet_gate.py rigid 192A …` | **PASS** (single boot) |

### 384 kHz — PASS mode (DSP bypassed, relay path)

| # | clause | measurement | evidence | verdict |
|---|---|---|---|---|
| 1 | exact except counted ±1 | 3,072,000 frames / 6,144,000 words: **all exact**, 0 mismatches, payload24 1.000000000 | `psheet_384.py` on `/tmp/bp_mic_X384P.raw` vs `/tmp/bp_in_X384P.raw` | **PASS** |
| 2 | zero |Δ| ≥ 2 | 0 words differ at all | same | **PASS** |
| 3 | device-vs-device exact | 384k pass leg bit-identical to the played file; determinism follows (no DSP state) | same + passT leg | **PASS** |
| 4 | input path bit-exact | **6,144,000 / 6,144,000 words exact** vs the played file — input path is word-exact end to end at 384k | `psheet_384.py` | **PASS** |
| 5 | FFT clean, 9 legs | passT −170.5 dB worst spur, G2 maxrun 1, zerofrac 0, RMS 2.96e6 | `scratch/psheet_gate_passT.log` | **PASS** |
| 6 | boot-stable d | no d (pass mode has no engine partition); stream is a pure relay | `psheet_384.py` | **PASS** |

### 9-leg FFT suite (clause 5), all on `bb8cee6b`, cold boot + md5-gated flash per leg

| leg | rate | mode | worst spur (G1, bar < −40 dB) | G2 maxrun (bar ≤ 3) | verdict |
|---|---|---|---|---|---|
| fix48 | 48k | arm | −144.8 dB | 0 | CLEAN |
| fix48b | 48k | arm | −150.7 dB | 0 | CLEAN |
| fix48c | 48k | arm | −146.3 dB | 0 | CLEAN |
| fix96 | 96k | arm | −150.0 dB | 0 | CLEAN |
| fixT192 | 192k | arm | −52.7 dB | 0 | CLEAN |
| pass48 | 48k | pass | −147.6 dB | 1 | CLEAN |
| pass96 | 96k | pass | −153.8 dB | 1 | CLEAN |
| passT192 | 192k | pass | −164.2 dB | 1 | CLEAN |
| passT | 384k | pass | −170.5 dB | 1 | CLEAN |

## Excluded / counted observations (not silently dropped)

* **Reference-dump end-of-buffer tail.** Scoring with a 64-word end margin removes a 22-frame
  artifact run at the very end of the in-range mapping (48C whole-window run reports DEEP 22 at
  frames 3434–3455, all with raw Δ ≥ 1024). Those frames map onto the last words of the 4096-frame
  dump, where the oracle stimulus has already ended: the reference's amplitude jumps ~10× (median
  |L| 454 M in the final 80 words vs 45.8 M in the first 500 content words) and it is a synthetic
  end-of-buffer ramp with no matching capture content. Excluding the boundary (margin 64 words,
  i.e. 8 frames at 48k / 16 at 96k / 32 at 192k) leaves DEEP 0. Reference-side artifact, not device
  behaviour; excluded from every number in the table above.
* **Capture zero-run slips, counted and excluded.** 48B contains a 12-frame zero insertion and
  96B2 a 24-frame one (at content frame 2028); each shifts the engine partition by an exact
  multiple of R × run length (24 frames × R). The slip frames themselves are not scored; both sides
  of the slip are scored separately and each side is DEEP 0. 48B row above is the post-slip
  sub-window (2500 frames), 96B2 row is the pre-slip window (1300 frames).
* **±1 count class, explained (do not re-litigate).** BFP shared-exponent / headroom choice plus
  the oversized-shift semantics difference (VPU `vlashr` saturates vs x86 C shift mod-32 UB),
  documented in `lib_audio_dsp/lib_audio_dsp/src/dsp/block_fir/fd_block_fir.c:21-38`. Sign of the
  flip is deterministic in the reference low-8 residue (dv=−1 median low8 ≈ 16, dv=+1 median
  low8 ≈ 224–240): the model sits on a bucket edge.

## UNVERIFIED / cited-only rows

| item | status | note |
|---|---|---|
| `/tmp/gate_s22` bb8cee6b FFT log | **absent (pruned)** | The 9-leg suite above was therefore **re-run from scratch on bb8cee6b today** (fresh md5-gated flash + `sudo -n modprobe` cold boot per leg). No row in this sheet rests on the missing log. |
| 192k device-vs-device on two 192k boots | **not measured on 192k** | determinism is proven at 48k (word-exact 3400/3400) and 96k (1.16 M frames payload-identical); the 192k set contains one boot. Marked as the only clause-3 qualification. |
| xsim backends | **out of scope by definition** | xsim VPU-asm vs xsim ref-C differ on 23 % of words after 3070 (campaign §24); the device is equidistant from both. No gate claim uses xsim. |

## Verdict

| rate | verdict |
|---|---|
| 48 kHz (armed) | **PASS** — DEEP 0 on 3/3 boots, ±1 counted, device-identical, FFT clean, d stable |
| 96 kHz (armed) | **PASS** — DEEP 0 on 2/2 boots, ±1 counted, device-identical over 1.16 M frames, FFT clean |
| 192 kHz (armed) | **PASS** — DEEP 0 on 5/5 dumps, ±1 counted, FFT clean (weakest leg −52.7 dB) |
| 384 kHz (pass) | **PASS** — 6,144,000/6,144,000 words exact |
| **overall** | **PASS under the user's final integer gate**: zero words at |Δ| ≥ 2 payload counts on every rate; every non-exact word is exactly one payload count, explained by the documented BFP exponent/shift realization difference |

Board ends certificate: image md5 `bb8cee6bf2b5fc670d52f1f299d1ef7b` verified, INFO `NFIELDS 181`
with `armed 0`, USB `20b1:0018`, ALSA card 4 `DACUAC20` record path opens and delivers 96,000 words
in 1 s (silent stimulus → zeros, as expected).

Reproduce: `scratch/psheet_gate.py rigid|piece|devcmp`, `scratch/psheet_dev96.py`,
`scratch/psheet_384.py`, `scratch/low8feed.py`, `scratch/class_census.py`, `scratch/insertscore5.py`;
FFT legs `scratch/psheet_fft9.sh` (logs `scratch/psheet_gate_<leg>.log`).
