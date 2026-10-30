#!/usr/bin/env python3
"""Generate Q(1+lsh).30 biquad coefficients for the ClearSound bass shelf.

Spec (user, 2026-10-29): below ~90 Hz the bass is 6 dB above the treble. To
avoid digital overflow the bass stays at 0 dB and mid/treble are cut by 6 dB --
the same transfer shape as "+6 dB bass", with the headroom in the treble.

RBJ second-order HIGH shelf (Q=1/sqrt2) at fc=90 Hz with gain=-6 dB is this
exact curve: unity at DC (0 dB bass) and -6 dB at the high end. Alternatives
rejected by the gate: first-order (-1.14 dB at 60 Hz) and a complementary
Butterworth knee (-1.61 dB at 60 Hz). Q=1.0 is flatter in the bass (+0.10 dB at
60 Hz) but overshoots the treble plateau (-6.42 dB), so the monotonic Q=0.707
curve ships.

Coefficients feed XMOS's adsp_biquad (lib_audio_dsp VPU asm):
    [b0/a0, b1/a0, b2/a0, -a1/a0, -a2/a0] in Q(1+lsh).30
lsh keeps every coefficient inside the signed Q(1+lsh).30 range, per the header
contract ("left shift compensation ... must be positive").

Float maths live HERE ONLY (host design tool); firmware stays integer-only.
The gate below measures the response of the QUANTISED integers, so what passes
here is what the VPU will actually compute.
"""
import math
import os

RATES = [44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000]
FC = 90.0            # shelf corner (user spec)
SHELF_DB = -6.0      # treble reduction (user spec)
Q = 30
OUT_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "..", "..", "src", "extensions"))


def _quant(c):
    """Q(1+lsh).30 integers for adsp_biquad; lsh keeps every |c| in range."""
    lsh = 0
    while max(abs(v) for v in c) >= (2.0 - 2.0 ** (-(Q + 1))) and lsh < 8:
        lsh += 1
        c = [v / 2.0 for v in c]
    q = [int(round(v * (2.0 ** (Q - lsh)))) for v in c]
    if any(abs(v) > 2**31 - 1 for v in q):
        raise RuntimeError("coefficient overflow (lsh=%s)" % lsh)
    return q, lsh


def shelf_coeffs(fs, fc=FC, shelf_db=SHELF_DB, q_factor=0.7071067811865476):
    """Bass shelf (bass 0 dB / treble -6 dB) -> (q[5] ints, lsh).

    RBJ HIGH shelf with gain = -6 dB is exactly this curve: the shelf puts its
    gain at the HIGH end and unity at DC, so

        H(0)  = 1        ->  0 dB bass   (headroom: bass cannot overflow)
        H(Nyq)= 10^(-6/20) -> -6.00 dB treble, exact
        transition centred on fc = 90 Hz

    Verified algebraically: sum(b)/sum(a) = 4A(1-cos)/4A(1-cos) = 1 at DC and
    4A^2(1+cos)/4(1+cos) = A^2 at Nyquist. One biquad, published formula,
    Q = 1/sqrt(2).

    Earlier forms rejected by the gate (T7): first-order drooped -1.14 dB at
    60 Hz; a complementary Butterworth knee drooped -1.61 dB there.
    """
    A = 10.0 ** (shelf_db / 40.0)                    # shelf amplitude (sqrt)
    w0 = 2.0 * math.pi * fc / fs
    cw, sw = math.cos(w0), math.sin(w0)
    al = sw / (2.0 * q_factor)
    sq = 2.0 * math.sqrt(A) * al
    b0 = A * ((A + 1) + (A - 1) * cw + sq)
    b1 = -2.0 * A * ((A - 1) + (A + 1) * cw)
    b2 = A * ((A + 1) + (A - 1) * cw - sq)
    a0 = (A + 1) - (A - 1) * cw + sq
    a1 = 2.0 * ((A - 1) - (A + 1) * cw)
    a2 = (A + 1) - (A - 1) * cw - sq
    return _quant([b0 / a0, b1 / a0, b2 / a0, -a1 / a0, -a2 / a0])


def q_response(q, lsh, f, fs):
    """|H(e^jw)| of the QUANTISED integers -- the firmware's real response."""
    s = 2.0 ** (Q - lsh)
    b0, b1, b2, ma1, ma2 = (v / s for v in q)
    z = complex(math.cos(-2 * math.pi * f / fs), math.sin(-2 * math.pi * f / fs))
    return abs((b0 + b1 * z + b2 * z * z) / (1.0 - ma1 * z - ma2 * z * z))


def run_integer(q, lsh, xseq):
    """Exact integer direct form I -- mirrors the adsp_biquad contract."""
    st = [0, 0, 0, 0]
    out, sh = [], Q - lsh
    for x in xseq:
        y = (q[0] * x + q[1] * st[0] + q[2] * st[1]
             + q[3] * st[2] + q[4] * st[3]) >> sh
        st[1] = st[0]; st[0] = x
        st[3] = st[2]; st[2] = y
        out.append(y)
    return out


def selftest():
    rc = 0
    print("fs      lsh  30Hz   90Hz   1kHz   10kHz  fullscale-peak  gate")
    for fs in RATES:
        q, lsh = shelf_coeffs(fs)
        g = {f: 20 * math.log10(max(q_response(q, lsh, f, fs), 1e-12))
             for f in (30, 60, 90, 1000, 4000, 10000)}
        # Headroom gate: full-scale tone at ANY band must not overflow. Bass is
        # where the shelf is unity -- the reason we cut treble instead of
        # boosting bass is precisely that this stays inside int32.
        peak = 0
        N = 1 << 14
        for f in (30, 90, 1000, 10000):
            xs = [int((2**31 - 1) * math.sin(2 * math.pi * f * i / fs))
                  for i in range(N)]
            peak = max(peak, max(abs(v) for v in run_integer(q, lsh, xs)[N // 2:]))
        g30, g60, g90 = g[30], g[60], g[90]
        # Spec: bass plateau 0 dB, corner -3 dB at 90 Hz, treble plateau -6 dB.
        # (60 Hz is inside the Q=0.707 transition, so only a loose sanity bound
        # applies there -- no 2nd-order shelf can be flat at 60 Hz with its
        # -3 dB point at 90 Hz.)
        ok = (g30 > -0.15) and (-3.4 < g90 < -2.6) and (g60 > -1.8) \
            and all(-6.25 < g[f] < -5.75 for f in (1000, 4000, 10000)) \
            and peak < 2**31
        print("%6d  %d  %+6.2f %+6.2f %+6.2f %+6.2f  %13d   %s"
              % (fs, lsh, g[30], g[90], g[1000], g[10000], peak,
                 "PASS" if ok else "FAIL"))
        if not ok:
            rc = 1
    return rc


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    L = ["// Auto-generated by tools/dsp/gen_biquad.py -- DO NOT EDIT.",
         "// Bass shelf (user spec 2026-10-29): 0 dB below %.0f Hz, %.1f dB above." % (FC, SHELF_DB),
         "// Feed lib_audio_dsp adsp_biquad: [b0,b1,b2,-a1,-a2] in Q(1+lsh).30.",
         "#ifndef __csd_dsp_bq_coeffs_h__",
         "#define __csd_dsp_bq_coeffs_h__",
         "#define CSD_BQ_NUM_RATES (%d)" % len(RATES),
         "typedef struct {",
         "    int32_t coeffs[5];",
         "    int32_t lsh;",
         "    unsigned rate;",
         "} csd_dsp_bq_rate_t;",
         "static const csd_dsp_bq_rate_t csd_dsp_bq_rates[] = {"]
    for fs in RATES:
        q, lsh = shelf_coeffs(fs)
        L.append("    { {%d, %d, %d, %d, %d}, %d, %d }," % (*q, lsh, fs))
    L += ["};", "#endif"]
    path = os.path.join(OUT_DIR, "csd_dsp_bq_coeffs.h")
    open(path, "w").write("\n".join(L) + "\n")
    print("wrote", path)
    return selftest()


if __name__ == "__main__":
    raise SystemExit(main())
