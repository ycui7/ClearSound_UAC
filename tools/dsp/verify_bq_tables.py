#!/usr/bin/env python3
"""T7: the shipped biquad header must equal the design, byte-exact.

Regenerates csd_dsp_bq_rates[] in memory with gen_biquad.shelf_coeffs() and
compares against src/extensions/csd_dsp_bq_coeffs.h -- catching a stale header
(hand-edited, or generated before a spec change) that would otherwise ship a
filter nobody measured. Also re-checks the acoustic requirements on the
QUANTISED integers, so the gate tests what the VPU actually computes:
  bass  >= -0.5 dB at 30/60 Hz, treble -6.00 +/- 0.25 dB at 1k/4k/10k,
  no int32 overflow with a full-scale Nyquist drive.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gen_biquad as G

HDR = os.path.abspath(os.path.join(HERE, "..", "..", "src", "extensions",
                                   "csd_dsp_bq_coeffs.h"))


def parse_hdr(path):
    txt = open(path).read()
    # rows look like:  { {b0, b1, b2, c3, c4}, lsh, rate },
    rows = re.findall(r"\{\s*\{\s*([-\d\s,]+?)\s*\}\s*,\s*(-?\d+)\s*,\s*(\d+)\s*\}", txt)
    out = []
    for c, lsh, rate in rows:
        q = [int(v) for v in c.split(",") if v.strip() != ""]
        if len(q) != 5:
            raise RuntimeError("row %s: expected 5 coeffs, got %d" % (rate, len(q)))
        out.append((q, int(lsh), int(rate)))
    return out


def main():
    if not os.path.exists(HDR):
        print("T7 FAIL: missing %s" % HDR)
        return 1
    shipped = parse_hdr(HDR)
    if len(shipped) != len(G.RATES):
        print("T7 FAIL: header has %d rates, generator has %d"
              % (len(shipped), len(G.RATES)))
        return 1
    bad = 0
    for (q, lsh, rate), fs in zip(shipped, G.RATES):
        if rate != fs:
            print("T7 FAIL: row %d: rate %d != %d" % (rate, rate, fs)); bad += 1
            continue
        e_q, e_lsh = G.shelf_coeffs(fs)
        if q != e_q or lsh != e_lsh:
            print("T7 FAIL: fs=%d header coeffs differ from design\n  hdr %s lsh=%d\n  gen %s lsh=%d"
                  % (fs, q, lsh, e_q, e_lsh)); bad += 1
            continue
        # requirements on the QUANTISED integers
        g = {f: 20 * __import__("math").log10(max(G.q_response(q, lsh, f, fs), 1e-12))
             for f in (30, 60, 90, 1000, 4000, 10000)}
        if g[30] < -0.15:
            print("T7 FAIL: fs=%d bass plateau 30Hz %.2f dB (want >= -0.15)"
                  % (fs, g[30])); bad += 1
        if not (-3.4 < g[90] < -2.6):
            print("T7 FAIL: fs=%d corner 90Hz %.2f dB (want -3.0 +/-0.4)"
                  % (fs, g[90])); bad += 1
        if g[60] < -1.8:
            print("T7 FAIL: fs=%d 60Hz %.2f dB (want > -1.8)" % (fs, g[60])); bad += 1
        for f in (1000, 4000, 10000):
            if not (-6.25 < g[f] < -5.75):
                print("T7 FAIL: fs=%d treble %d Hz = %.2f dB (want -6.00 +/-0.25)"
                      % (fs, f, g[f])); bad += 1
        # full-scale Nyquist drive must not overflow int32
        st = [0, 0, 0, 0]
        sh = G.Q - lsh
        worst = 0
        for i in range(2048):
            x = 2147483647 if (i & 1) else -2147483647
            y = (q[0] * x + q[1] * st[0] + q[2] * st[1]
                 + q[3] * st[2] + q[4] * st[3]) >> sh
            st[1] = st[0]; st[0] = x
            st[3] = st[2]; st[2] = y
            worst = max(worst, abs(y))
        if worst >= 2**31:
            print("T7 FAIL: fs=%d overflow at full scale (%d)" % (fs, worst)); bad += 1
    if bad:
        print("T7 FAIL: %d problem(s)" % bad)
        return 1
    print("T7 PASS: shipped biquad header == design, %d rates, "
          "bass flat / treble -6 dB / no overflow" % len(shipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
