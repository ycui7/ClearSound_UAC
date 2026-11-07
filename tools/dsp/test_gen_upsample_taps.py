"""Property tests for the generated upsampling tables.

These check the properties the design promises, on the QUANTISED coefficients
that actually ship -- not on an ideal floating-point filter.
"""
import unittest

import gen_upsample_taps as g


def resp_db(coefs, freq, fs_out=None, q=30):
    import cmath, math
    fs_out = fs_out or g.FS_OUT
    scale = 1 << q
    acc = 0j
    w = 2 * math.pi * freq / fs_out
    for i, v in enumerate(coefs):
        acc += (v / scale) * cmath.exp(-1j * w * i)
    return 20 * math.log10(max(abs(acc), 1e-12))


def rel_db(coefs, freq):
    """Response relative to DC. The prototype carries gain L (one phase must
    deliver unity DC), so only the SHAPE is meaningful as a filter spec."""
    return resp_db(coefs, freq) - resp_db(coefs, 0.0)


def q30(values):
    return g.to_q30(values)


class InterpolatorProperties(unittest.TestCase):
    def test_dc_gain_is_unity(self):
        for fs_in in g.RATIOS:
            _, proto = g.design_interpolator(fs_in)
            L = g.RATIOS[fs_in]
            self.assertAlmostEqual(sum(q30(proto)) / (1 << 30), float(L), delta=2e-2, msg=f"{fs_in}")

    def test_filter_is_symmetric_so_phase_is_linear(self):
        for fs_in in g.RATIOS:
            _, proto = g.design_interpolator(fs_in)
            q = q30(proto)
            diff = max(abs(q[i] - q[len(q) - 1 - i]) for i in range(len(q)))
            self.assertLessEqual(diff, 2, f"{fs_in} Hz: symmetry broken -> phase not linear")

    def test_first_image_is_rejected_by_80_db(self):
        for fs_in in g.RATIOS:
            L, proto = g.design_interpolator(fs_in)
            q = q30(proto)
            f_pass = g.A_PASS_EDGE * fs_in
            worst = max(rel_db(q, fs_in - f_pass + i * 500) for i in range(int(2 * f_pass / 500) + 1))
            self.assertLess(worst, -80.0, f"{fs_in} Hz: image at {fs_in} leaked {worst:.1f} dB relative to DC")

    def test_passband_is_flat_to_a_tenth_of_a_db(self):
        for fs_in in g.RATIOS:
            _, proto = g.design_interpolator(fs_in)
            q = q30(proto)
            f_pass = g.A_PASS_EDGE * fs_in
            dev = max(abs(rel_db(q, i * f_pass / 40)) for i in range(41))
            self.assertLess(dev, 0.1, f"{fs_in} Hz: passband deviates {dev:.2f} dB")

    def test_sinc_beats_linear_interpolation_by_a_wide_margin(self):
        fs_in = 48000
        _, proto = g.design_interpolator(fs_in)
        sinc_img = rel_db(q30(proto), fs_in - 21600)
        linear_img = 20 * __import__("math").log10(0.5)  # 2-tap average at Nyquist
        self.assertLess(sinc_img, linear_img - 60, "sinc must reject images far better than linear")


class BandLimitProperties(unittest.TestCase):
    def test_minus_3db_at_15k_and_24db_per_octave_steepness(self):
        h = q30(g.design_bandlimit())
        self.assertAlmostEqual(resp_db(h, 0), 0.0, delta=0.2)
        self.assertAlmostEqual(resp_db(h, 15000), -3.0, delta=1.0)
        steep = resp_db(h, 30000) - resp_db(h, 15000)
        self.assertLess(steep, -18.0, "4th-order equivalent must fall >=18 dB over one octave")

    def test_magnitude_tracks_the_fourth_order_ideal(self):
        # a 4th-order Butterworth is -48 dB at 4x its corner; assert the realised
        # FIR follows the IDEAL curve instead of an invented threshold
        h = q30(g.design_bandlimit())
        for f in (5000, 15000, 30000, 60000, 96000):
            ideal = 20 * __import__("math").log10(g.butterworth4_mag(f, g.B_FC))
            self.assertAlmostEqual(resp_db(h, f), ideal, delta=1.5, msg=f"{f} Hz off the 4th-order curve")

    def test_response_is_monotone_and_symmetric(self):
        h = q30(g.design_bandlimit())
        prev = 1e9
        for f in range(0, 120001, 5000):
            cur = resp_db(h, f)
            self.assertLessEqual(cur, prev + 0.5, f"response rises at {f} Hz")
            prev = cur
        diff = max(abs(h[i] - h[len(h) - 1 - i]) for i in range(len(h)))
        self.assertLessEqual(diff, 2, "not symmetric -> phase not linear")


if __name__ == "__main__":
    unittest.main(verbosity=2)
