// Host gate for the function generator (src/extensions/csd_gen_core.h).
//
// Compiled on the dev box against the SAME header the firmware compiles, the
// arrangement tools/dsp/test_ring.cpp uses. tools/dsp/test_gen.sh builds and
// runs it; a failure here stops the change before it reaches the target.
//
// DESIGN OF THE TESTS
// "Produces samples" is not a property worth asserting -- any loop does that.
// Each test asserts something a plausible-looking implementation breaks:
//
//   * canaries. Three deliberate wrong implementations (T3 naive square, T4
//     recurrent log sweep, T5 un-normalised multitone) are asserted to FAIL
//     the same assertions. An assertion no wrong code ever fails is
//     decoration, and this suite has already shipped two of those.
//   * the sweep is checked against an independent double-precision
//     expectation at several points, and separately by the audio it actually
//     produces. The recurrent implementation this replaced hit both endpoints
//     exactly and was wrong throughout the middle, so endpoints alone are
//     not a test.
//   * all three directions (f_start >, <, = f_stop) must agree with the same
//     expectation, because direction is not supposed to be a case.
//   * spectra are measured only at frequencies coherent with the window
//     (integer hertz, integer-second windows). The first version of this gate
//     used arbitrary frequencies with rectangular windows and blamed the
//     firmware for the leakage: it reported 55 failures, every one of them
//     the test's fault.
//   * noise is judged on bin-AVERAGED band energy. A single DFT bin of white
//     noise is Rayleigh-distributed with ~5 dB standard deviation (measured
//     here: -16 dB to +7 dB), so a per-bin tolerance either always fails or
//     has to be loosened until it asserts nothing.
//
// Build: g++ -O2 -std=c++11 -I<src/extensions> test_gen.cpp -o /tmp/test_gen

#include <cstdio>
#include <cstdarg>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <algorithm>
#include "csd_gen_core.h"

static int fails = 0;
static int checks = 0;

static void ok(bool cond, const char *what)
{
    checks++;
    if (!cond) { fails++; printf("  FAIL  %s\n", what); }
}

static void okf(bool cond, const char *fmt, ...)
{
    checks++;
    if (!cond) {
        va_list ap; va_start(ap, fmt);
        printf("  FAIL  "); vprintf(fmt, ap); printf("\n");
        va_end(ap);
        fails++;
    }
}

/* ------------------------------------------------------------- analysis */

/* Goertzel magnitude: 2|X|/N, so a full-scale sine of amplitude A reads A. */
static double goertzel(const std::vector<int32_t> &x, double f, double fs)
{
    double w = 2.0 * M_PI * f / fs;
    double cw = cos(w), coeff = 2.0 * cw;
    double s0 = 0, s1 = 0, s2 = 0;
    for (size_t i = 0; i < x.size(); i++) {
        s0 = (double)x[i] / 2147483648.0 + coeff * s1 - s2;
        s2 = s1; s1 = s0;
    }
    double re = s1 - s2 * cw;
    double im = s2 * sin(w);
    return 2.0 * hypot(re, im) / (double)x.size();
}

static double db(double a, double ref)
{
    if (a <= 1e-18 || ref <= 1e-18) return -340.0;
    return 20.0 * log10(a / ref);
}

/* Zero-crossing frequency estimate over [a,b): half a crossing per cycle.
 *
 * Exact-zero samples must CARRY the previous sign rather than count as
 * positive. Comparing `x[i] < 0` directly treats a sample that lands on
 * exactly 0 as non-negative, which can merge two crossings into one or invent
 * an extra one; measured, that made a 1000 Hz tone read as 999 Hz over a
 * half-second window and failed a 0.6 Hz tolerance. The generator was fine --
 * the counter was wrong, and the fix belongs in the counter. */
static double zc_hz(const std::vector<int32_t> &x, size_t a, size_t b, double fs)
{
    if (b <= a + 2) return 0.0;
    size_t crossings = 0;
    int prev_sign = 0;                      /* 0 = unknown yet */
    for (size_t i = a; i < b; i++) {
        int sign = (x[i] < 0) ? -1 : (x[i] > 0 ? 1 : 0);
        if (sign == 0) continue;            /* keep the last known sign */
        if (prev_sign != 0 && sign != prev_sign) crossings++;
        prev_sign = sign;
    }
    return crossings * 0.5 * fs / (double)(b - a);
}

/* Coherent window: an integer number of periods, exactly.
 *
 * Valid because every spectral assertion here uses integer hertz at an
 * integer sample rate over an integer number of seconds, so cycles =
 * seconds*f is exact and the tone lands on a DFT bin -- a rectangular window
 * then leaks nothing. Every spectral check goes through here, because
 * leakage from a non-coherent window showed up as spurious harmonics and DC
 * in the first version of this gate. */
static size_t coherent_n(double f_hz, double fs, double seconds)
{
    double per = fs / f_hz;
    if (per < 1.0) per = 1.0;
    size_t cycles = (size_t)llround(seconds * f_hz);
    if (cycles < 1) cycles = 1;
    size_t n = (size_t)llround(per * (double)cycles);
    if (n < 4096) n = 4096;
    if (n > 4000000) n = 4000000;
    return n;
}

static const double FS_DEF = 48000.0;
static const uint32_t AMP_DEF = 0x40000000u;      /* -6 dBFS */

static int arm(csd_gen_t &g, unsigned fs, unsigned mode, double f_a, double f_b,
               unsigned domain, uint32_t amp, uint64_t period_ms)
{
    csd_gen_reset(&g);
    int why = -1;
    int r = csd_gen_wave(&g, fs, mode,
                         (uint32_t)llround(f_a * CSD_GEN_HZ_ONE),
                         (uint32_t)llround(f_b * CSD_GEN_HZ_ONE),
                         domain, amp, period_ms, &why);
    return r ? -why : 0;
}

static void run(csd_gen_t &g, std::vector<int32_t> &out, size_t n)
{
    out.resize(n);
    int32_t s[2];
    for (size_t i = 0; i < n; i++) {
        csd_gen_sample(&g, s);
        out[i] = s[0];
    }
}

static void peak_of(const std::vector<int32_t> &x, int32_t &mx, int32_t &mn)
{
    mx = -2147483648; mn = 2147483647;
    for (size_t i = 0; i < x.size(); i++) {
        if (x[i] > mx) mx = x[i];
        if (x[i] < mn) mn = x[i];
    }
}

/* -------------------------------------------------------- T1 sine tone */
static void t1_sine(void)
{
    printf("T1 sine: amplitude, DC, harmonic floor\n");
    const unsigned rates[] = {44100u, 48000u, 96000u, 192000u};
    const double freqs[] = {1.0, 10.0, 100.0, 997.0, 5000.0, 10000.0, 20000.0};
    for (unsigned fs : rates) {
        for (double f : freqs) {
            if (f > fs / 2.4) continue;             /* keep Nyquist margin */
            csd_gen_t g;
            okf(arm(g, fs, CSD_GEN_SINE, f, f, CSD_GEN_LIN, AMP_DEF, 0) == 0,
                "sine %g Hz @%u rejected", f, fs);
            std::vector<int32_t> x;
            size_t n = coherent_n(f, fs, 2.0);
            run(g, x, n);

            double want = (double)AMP_DEF / 2147483648.0;      /* 0.5 */
            double sig = goertzel(x, f, fs);
            okf(fabs(db(sig, want)) < 0.5,
                "sine %g Hz @%u: amplitude %g dBFS (want %g)", f, fs,
                db(sig, 1.0), db(want, 1.0));

            double dc = goertzel(x, 0.0, fs);
            okf(db(dc, sig) < -95.0,
                "sine %g Hz @%u: DC %g dB (want < -95)", f, fs, db(dc, sig));

            /* 2nd harmonic catches an asymmetric or mis-indexed table, 3rd an
             * interpolation error. The table's own linear-interpolation floor
             * is ~-118 dB, so -95 is achievable and still meaningful. */
            for (int h = 2; h <= 3; h++) {
                if (f * h >= fs / 2.0) break;
                double a = goertzel(x, f * h, fs);
                okf(db(a, sig) < -95.0,
                    "sine %g Hz @%u: harmonic %d at %g dB (want < -95)",
                    f, fs, h, db(a, sig));
            }
            /* There is deliberately NO check of the bin at fs-f. For any
             * real-valued signal |X(fs-f)| == |X(f)| by DFT conjugate
             * symmetry, so an earlier revision of this test asserted the
             * image was 95 dB down and failed every case while printing
             * differences of 1e-13 dB -- the equality itself. A check that
             * contradicts a theorem is not a strict test, it is a wrong one. */
        }
    }
}

/* ------------------------------------------------- T2 produced frequency */
static void t2_freq_accuracy(void)
{
    printf("T2 produced frequency (rounding; the fs argument actually used)\n");
    /* The step must round, not truncate: truncation biases every tone low by
     * up to half a phase LSB, and the bias is systematic, not random. */
    double bias_sum = 0.0; int bias_n = 0;
    for (unsigned f = 1; f <= 20000; f += 997) {
        for (unsigned fs : {44100u, 48000u, 96000u, 192000u}) {
            if (f > fs / 2.4) continue;
            uint64_t ps = csd_gen_pstep(f << CSD_GEN_HZ_SHIFT, fs);
            double got = (double)ps / (double)CSD_GEN_HZ_ONE / 4294967296.0 * (double)fs;
            double err = got - (double)f;
            bias_sum += err; bias_n++;
            okf(fabs(err) < 0.005,
                "%u Hz @%u: produced %.6f (err %.2e)", f, fs, got, err);
        }
    }
    okf(fabs(bias_sum / bias_n) < 1e-4,
        "mean frequency bias %g -- truncation would make it consistently "
        "negative, not centred on zero", bias_sum / bias_n);

    /* THE fs ARGUMENT REGRESSION. The core used to read g->fs, which reset()
     * zeroes, so arming before the stream reported its rate failed and callers
     * learned to poke the field first. If the rate argument is ever ignored
     * again, every rate here produces the same tone and this fails. */
    for (unsigned fs : {44100u, 48000u, 96000u, 192000u}) {
        csd_gen_t g;
        okf(arm(g, fs, CSD_GEN_SINE, 1000.0, 1000.0, CSD_GEN_LIN, AMP_DEF, 0) == 0,
            "sine @%u rejected", fs);
        std::vector<int32_t> x;
        run(g, x, (size_t)(fs / 2));
        double z = zc_hz(x, 0, x.size(), (double)fs);
        /* One crossing over a 0.5 s window is 1 Hz, so the estimator cannot
         * resolve finer than that and the old 0.6 Hz bound was below the
         * quantum of the measurement -- a correct 1000 Hz tone read as 999
         * and failed. Tolerance = 1.5 crossings. */
        double q = fs / (2.0 * (double)x.size());
        okf(fabs(z - 1000.0) <= 1.5 * q,
            "sine @%u measured %g Hz (want 1000, quantum %g)", fs, z, q);
    }

    /* fs argument wins over a deliberately wrong struct field. */
    {
        csd_gen_t g; csd_gen_reset(&g);
        g.fs = 999999u;
        int why = -1;
        int r = csd_gen_wave(&g, 48000u, CSD_GEN_SINE, 1000u << 8, 1000u << 8,
                             CSD_GEN_LIN, AMP_DEF, 0, &why);
        ok(r == 0 && g.fs == 48000u, "csd_gen_wave did not adopt the fs argument");
        std::vector<int32_t> x;
        run(g, x, 24000);
        double z = zc_hz(x, 0, x.size(), 48000.0);
        double q = 48000.0 / (2.0 * (double)x.size());
        okf(fabs(z - 1000.0) <= 1.5 * q,
            "fs argument ignored: measured %g Hz (quantum %g)", z, q);
    }

    /* Rate change re-derives: 1000 Hz stays 1000 Hz at the new rate. */
    {
        csd_gen_t g;
        ok(arm(g, 48000u, CSD_GEN_SINE, 1000.0, 1000.0, CSD_GEN_LIN, AMP_DEF, 0) == 0,
           "pre-rate-change arm failed");
        ok(csd_gen_set_rate(&g, 96000u) == 0, "set_rate rejected a legal change");
        std::vector<int32_t> x;
        run(g, x, 48000);
        double z = zc_hz(x, 0, x.size(), 96000.0);
        double q = 96000.0 / (2.0 * (double)x.size());
        okf(fabs(z - 1000.0) <= 1.5 * q,
            "after set_rate(96000): %g Hz (want 1000, quantum %g)", z, q);

        /* An illegal new rate is refused AND leaves the old config usable. */
        csd_gen_t h;
        ok(arm(h, 192000u, CSD_GEN_SINE, 20000.0, 20000.0, CSD_GEN_LIN, AMP_DEF, 0) == 0,
           "20k@192k arm failed");
        ok(csd_gen_set_rate(&h, 32000u) != 0,
           "set_rate accepted 20 kHz at 32 kHz (above Nyquist)");
        ok(h.fs == 192000u, "rejected set_rate left the rate changed");
    }
}

/* --------------------------------------------- T3 square + aliasing */

/* Deliberately wrong: the naive sign of the phase. Its harmonics run to
 * infinity and fold back into band. Asserted to FAIL the alias test below,
 * which is what gives that test teeth. */
static void naive_square(csd_gen_t &g, std::vector<int32_t> &out, size_t n)
{
    out.resize(n);
    uint32_t ph = 0;
    uint64_t ps = csd_gen_pstep(g.f_a, g.fs);
    uint64_t frac = 0;
    for (size_t i = 0; i < n; i++) {
        frac += ps & 0xFFull;
        ph += (uint32_t)(ps >> 8) + (uint32_t)(frac >> 8);
        frac &= 0xFFull;
        out[i] = (ph & 0x80000000u) ? (int32_t)g.amp : -(int32_t)g.amp;
    }
}

static void t3_square(void)
{
    printf("T3 square: odd-harmonic series, even suppression, alias floor\n");
    const double freqs[] = {1.0, 10.0, 100.0, 1000.0, 5000.0};
    for (double f : freqs) {
        csd_gen_t g;
        okf(arm(g, 48000u, CSD_GEN_SQUARE, f, f, CSD_GEN_LIN, AMP_DEF, 0) == 0,
            "square %g Hz rejected", f);
        std::vector<int32_t> x;
        run(g, x, coherent_n(f, FS_DEF, 2.0));

        double fund = goertzel(x, f, FS_DEF);
        okf(fund > 0.20, "square %g Hz: fundamental %g (want ~0.5)", f, fund);

        /* Odd harmonics follow 1/n. Ratios are checked rather than absolute
         * levels because the band limit truncates at a different harmonic
         * count per frequency; the ratios hold for any truncation. */
        for (int h = 3; h <= 15; h += 2) {
            double fh = f * h;
            if (fh >= FS_DEF / 2.0) break;
            double a = goertzel(x, fh, FS_DEF);
            double want = fund / h;
            okf(fabs(db(a, want)) < 1.0,
                "square %g Hz harmonic %d: %g dB from ideal 1/n", f, h,
                db(a, want));
        }
        /* Even harmonics are absent from a square by symmetry. Half-cycle
         * asymmetry -- the usual bug in a hand-rolled generator -- lands
         * squarely here. */
        for (int h = 2; h <= 10; h += 2) {
            double fh = f * h;
            if (fh >= FS_DEF / 2.0) break;
            double a = goertzel(x, fh, FS_DEF);
            okf(db(a, fund) < -95.0,
                "square %g Hz even harmonic %d: %g dB (want < -95)",
                f, h, db(a, fund));
        }
        int32_t mx, mn; peak_of(x, mx, mn);
        okf(std::max(mx, -mn) <= (int32_t)AMP_DEF + 1000,
            "square %g Hz peak %d exceeds amplitude %u", f,
            std::max(mx, -mn), AMP_DEF);
    }

    /* THE ALIAS TEST. A 20 kHz square's 3rd harmonic is 60 kHz, which folds to
     * |48000-60000| = 12 kHz. A hard-switched square puts that spur about
     * -9.5 dB below the fundamental: a DUT measured against it would be
     * measured against its own aliased artefacts and they would look like
     * device behaviour. The harmonic-sum construction drops the 3rd rather
     * than folding it, so this bin must be empty. */
    {
        const double f = 20000.0;
        csd_gen_t g;
        ok(arm(g, 48000u, CSD_GEN_SQUARE, f, f, CSD_GEN_LIN, AMP_DEF, 0) == 0,
           "20k square arm failed");
        std::vector<int32_t> x;
        run(g, x, 480000);
        double fund = goertzel(x, f, FS_DEF);
        double spur = goertzel(x, 12000.0, FS_DEF);
        okf(fund > 0.2, "20k square fundamental %g", fund);
        okf(db(spur, fund) < -95.0,
            "20k square: folded 12 kHz spur at %g dB (want < -95)",
            db(spur, fund));

        /* CANARY: the naive square must blow the same assertion. If it does
         * not, the assertion above has no teeth and the test is theatre --
         * which is how a broken polyBLEP nearly shipped here. */
        csd_gen_t n;
        ok(arm(n, 48000u, CSD_GEN_SQUARE, f, f, CSD_GEN_LIN, AMP_DEF, 0) == 0,
           "naive arm failed");
        std::vector<int32_t> xn;
        naive_square(n, xn, 480000);
        double nfund = goertzel(xn, f, FS_DEF);
        double nspur = goertzel(xn, 12000.0, FS_DEF);
        okf(db(nspur, nfund) > -20.0,
            "SELF-CHECK: naive square folded spur only %g dB -- the alias "
            "assertion cannot fail and is worthless", db(nspur, nfund));
        printf("      (naive %g dB vs band-limited %g dB: the gap is what the "
               "harmonic sum buys)\n", db(nspur, nfund), db(spur, fund));
    }

    /* Direction must not be a case: equal endpoints under either domain, and
     * with a period supplied, produce the same wave. */
    {
        std::vector<int32_t> up, dn, st;
        csd_gen_t a, b, c;
        arm(a, 48000u, CSD_GEN_SQUARE, 3000.0, 3000.0, CSD_GEN_LIN, AMP_DEF, 0);
        run(a, up, 96000);
        arm(b, 48000u, CSD_GEN_SQUARE, 3000.0, 3000.0, CSD_GEN_LOG, AMP_DEF, 0);
        run(b, dn, 96000);
        arm(c, 48000u, CSD_GEN_SQUARE, 3000.0, 3000.0, CSD_GEN_LIN, AMP_DEF, 1000);
        run(c, st, 96000);
        double f1 = goertzel(up, 3000.0, FS_DEF);
        okf(fabs(db(goertzel(dn, 3000.0, FS_DEF), f1)) < 0.1,
            "square differs between domains at equal endpoints");
        okf(fabs(db(goertzel(st, 3000.0, FS_DEF), f1)) < 0.1,
            "square differs when a period is supplied with equal endpoints");
    }
}

/* ---------------------------------------------------------- T4 sweeps */

/* Independent expectation, double precision, written from the definition of
 * a linear and a log sweep rather than from the implementation. */
static double expect_hz(unsigned domain, double fa, double fb, double u)
{
    if (fa == fb) return fa;
    if (domain == CSD_GEN_LIN) return fa + (fb - fa) * u;
    return fa * pow(fb / fa, u);
}

static void sweep_audio(const char *label, double fa, double fb, unsigned domain,
                        double period_s, double fs)
{
    csd_gen_t g;
    /* period_s is SECONDS here and the API takes MILLISECONDS. The two are
     * converted in exactly one place, and T0 below pins the units so getting
     * it backwards fails an assertion instead of silently measuring the wrong
     * window. An earlier revision passed seconds into the millisecond field:
     * a 2 s sweep armed as 2 ms, T collapsed to the clamp, every slice read
     * the window centre, and the failures looked like generator bugs. */
    uint64_t period_ms = (uint64_t)llround(period_s * 1000.0);
    if (arm(g, (unsigned)fs, CSD_GEN_SWEEP, fa, fb, domain, AMP_DEF,
            period_ms) != 0) {
        okf(false, "%s: arm rejected", label);
        return;
    }
    /* Pin the unit contract: N seconds at fs must be N*fs frames, within the
     * half-frame rounding. A seconds/milliseconds swap is off by 1000x and
     * cannot pass this. */
    {
        double want_frames = period_s * fs;
        double got = (double)g.period_frames;
        okf(fabs(got - want_frames) <= 1.0,
            "%s: period_frames %g, want %g for %g s @%g Hz -- unit mismatch "
            "between caller and csd_gen_wave", label, got, want_frames,
            period_s, fs);
    }
    size_t T = (size_t)g.period_frames;
    if (T < 64) T = 64;
    std::vector<int32_t> x;
    run(g, x, T + 10);

    /* Slice the audio and compare each slice's crossing rate with the formula
     * at the slice centre. Endpoints alone are not a test: the recurrent
     * implementation this replaced hit both endpoints exactly and spent the
     * middle of the sweep at the start frequency. */
    const double at[] = {0.05, 0.25, 0.50, 0.75, 0.95};
    for (double p : at) {
        size_t w = (size_t)(T * 0.08);
        if (w < 512) w = 512;
        size_t a = (size_t)(T * p);
        if (a + w > T) { a = (T > w) ? T - w : 0; w = T - a; }
        if (w < 64) continue;
        double meas = zc_hz(x, a, a + w, fs);
        double want = expect_hz(domain, fa, fb, (double)a / (double)T + 0.5 * w / (double)T);
        /* Resolution of the estimator over THIS slice: one crossing is
         * fs/(2w) Hz. A tone whose whole expected frequency is below a few
         * quanta cannot be counted in the slice at all -- "measured 0.00 Hz,
         * formula says 2.44 Hz" over a 40 ms window is arithmetic, not a
         * generator fault -- so the comparison is skipped and only the
         * low-frequency side is asserted. Deriving the cutoff from w rather
         * than hard-coding 2 Hz is what makes it correct at 0.5 s and 24 h. */
        double q = fs / (2.0 * (double)w);
        if (want < 4.0 * q) {
            okf(meas <= 4.0 * q,
                "%s: slice %g%% measured %g Hz (want ~%g, quantum %g)",
                label, p * 100.0, meas, want, q);
            continue;
        }
        okf(fabs(db(meas, want)) < 1.0,
            "%s: slice %g%% measured %.2f Hz, formula says %.2f Hz",
            label, p * 100.0, meas, want);
    }

    /* Monotonic in the requested direction. A sign error in the log exponent
     * produces a sweep that rises when asked to fall, and both endpoints can
     * look right if they are clamped. */
    const size_t K = 20;
    int rising = (fb > fa) ? 1 : (fb < fa ? -1 : 0);
    double prev = -1.0;
    for (size_t k = 0; k <= K; k++) {
        size_t idx = T * k / K;
        double f = csd_gen_frequency(&g, idx) / (double)CSD_GEN_HZ_ONE;
        if (prev >= 0.0) {
            if (rising > 0) okf(f >= prev - 1e-6, "%s: not rising at %g%%", label, k * 5.0);
            if (rising < 0) okf(f <= prev + 1e-6, "%s: not falling at %g%%", label, k * 5.0);
            if (rising == 0) okf(fabs(f - prev) < 1e-6, "%s: drifted on a steady request", label);
        }
        prev = f;
    }

    /* Endpoints, then held.
     *
     * "Exactly" means to the resolution the representation actually has, and
     * that has to be spelled out or the bound becomes folklore. Frequency is
     * Q8: 1/256 Hz per LSB, so 1 LSB cannot be the tolerance when the log
     * path computes the endpoint by round-tripping through the log2 and exp2
     * tables (measured worst case 1.9e-6 relative, ~2 LSB at 20 kHz). The
     * bound is 4 LSB, twice the measured round trip, and it is still a real
     * limit: the earlier revision turned a 4093 Hz tone into 2654 Hz, which
     * is nine orders of magnitude outside it. */
    const double tol_hz = 4.0 / CSD_GEN_HZ_ONE;      /* 1/64 Hz */
    double f0 = csd_gen_frequency(&g, 0) / (double)CSD_GEN_HZ_ONE;
    double fT = csd_gen_frequency(&g, T) / (double)CSD_GEN_HZ_ONE;
    double fT2 = csd_gen_frequency(&g, T + 1000000ull) / (double)CSD_GEN_HZ_ONE;
    okf(fabs(f0 - fa) <= tol_hz, "%s: start %.6f want %g (tol %g Hz)",
        label, f0, fa, tol_hz);
    okf(fabs(fT - fb) <= tol_hz + fb * 2e-6, "%s: end %.6f want %g", label, fT, fb);
    okf(fabs(fT2 - fb) <= tol_hz + fb * 2e-6,
        "%s: did not hold %g after the end (%.6f)", label, fb, fT2);
}

static void t4_sweep(void)
{
    printf("T4 sweeps: linear/log, rising/falling/steady, 0.5 s .. 24 h\n");
    const double periods[] = {0.5, 1.0, 5.0, 30.0, 3600.0};
    for (double p : periods) {
        for (unsigned dom : {CSD_GEN_LIN, CSD_GEN_LOG}) {
            char lbl[96];
            snprintf(lbl, sizeof lbl, "%s up   %gs", dom ? "log" : "lin", p);
            sweep_audio(lbl, 1.0, 20000.0, dom, p, FS_DEF);
            snprintf(lbl, sizeof lbl, "%s down %gs", dom ? "log" : "lin", p);
            sweep_audio(lbl, 20000.0, 1.0, dom, p, FS_DEF);
        }
    }
    /* The steady case must come from the same code as a sweep, not from a
     * special branch that can drift away from it. */
    sweep_audio("steady via sweep api", 440.0, 440.0, CSD_GEN_LOG, 10.0, 96000.0);

    /* Endpoints that broke the table: 4093 Hz has a log2 fraction of 0.9995
     * and came out at 2654 Hz while every exact power of two was perfect, so
     * a check on round frequencies alone passes and proves nothing. */
    struct { double a, b; unsigned fs; } odd[] = {
        {4093.0, 19999.0, 48000u}, {19999.0, 4093.0, 48000u},
        {86.0, 12345.0, 44100u},   {1.0, 1.0, 48000u},
        {20.0, 20000.0, 192000u},  {1023.0, 4097.0, 96000u},
    };
    for (auto &o : odd) {
        char lbl[96];
        snprintf(lbl, sizeof lbl, "odd %g->%g @%u", o.a, o.b, o.fs);
        for (unsigned dom : {CSD_GEN_LIN, CSD_GEN_LOG})
            sweep_audio(lbl, o.a, o.b, dom, 2.0, (double)o.fs);
    }

    /* Mid-sweep value must equal the closed form, not merely sit between the
     * endpoints: that is the property distinguishing log from linear. */
    for (unsigned dom : {CSD_GEN_LIN, CSD_GEN_LOG}) {
        csd_gen_t g;
        arm(g, 48000u, CSD_GEN_SWEEP, 1.0, 20000.0, dom, AMP_DEF, 30000);
        size_t T = (size_t)(30000.0 * 48000.0 / 1000.0);
        double mid = csd_gen_frequency(&g, T / 2) / (double)CSD_GEN_HZ_ONE;
        double want = expect_hz(dom, 1.0, 20000.0, 0.5);
        okf(fabs(db(mid, want)) < 0.05,
            "%s sweep midpoint %.3f Hz, closed form %.3f Hz",
            dom ? "log" : "linear", mid, want);
    }

    /* CANARY: the recurrent implementation this replaced -- multiply the step
     * by a constant ratio every frame. For a slow log sweep the product
     * rounds back to its own operand, so the tone never accelerates: a 30 s
     * sweep finished at 1 Hz. Asserted to fail the midpoint test, which is
     * what proves T4 can detect that regression rather than merely document
     * today's behaviour. */
    {
        unsigned fs = 48000u;
        size_t T = 30 * fs;
        uint32_t sa = (uint32_t)csd_gen_pstep(1u << CSD_GEN_HZ_SHIFT, fs);
        uint32_t sb = (uint32_t)csd_gen_pstep((uint32_t)llround(20000.0 * CSD_GEN_HZ_ONE), fs);
        double r = pow((double)sb / (double)sa, 1.0 / (double)T);
        uint64_t lratio = (uint64_t)llround(r * 4294967296.0);
        uint64_t step = sa;
        for (size_t i = 0; i < T / 2; i++) step = (step * lratio) >> 32;
        /* csd_gen_pstep returns the phase increment in Q8 (phase units x256),
         * so Hz = step / 256 / 2^32 * fs. The first version divided by 65536
         * and printed a midpoint of 0.04 Hz: the canary still "passed" while
         * describing a physically absurd number. A canary nobody checks is
         * worse than no canary, because it reads like coverage. */
        double got = (double)step / (double)CSD_GEN_HZ_ONE / 4294967296.0 * fs;
        double want = expect_hz(CSD_GEN_LOG, 1.0, 20000.0, 0.5);
        okf(fabs(db(got, want)) > 6.0,
            "SELF-CHECK: recurrent log sweep midpoint reads %.2f Hz (want "
            "%.2f) -- it does not look wrong, so if this passes, the suite has "
            "no way to detect the old bug", got, want);
        printf("      (recurrent midpoint %.2f Hz vs closed form %.2f Hz)\n",
               got, want);
    }

    /* Duration comes from the period argument, in frames, at every rate. */
    for (unsigned fs : {44100u, 48000u, 96000u, 192000u}) {
        csd_gen_t g;
        arm(g, fs, CSD_GEN_SWEEP, 1000.0, 20000.0, CSD_GEN_LIN, AMP_DEF, 500);
        double want_frames = 0.5 * fs;
        uint32_t target = (uint32_t)llround(20000.0 * CSD_GEN_HZ_ONE);
        double found = 0;
        for (double k = 1.0; k < 4.0; k += 0.01) {
            size_t idx = (size_t)(want_frames * k);
            if (csd_gen_frequency(&g, idx) <= target + 1u) { found = idx; break; }
        }
        okf(fabs(found - want_frames) / want_frames < 0.02,
            "@%u: sweep reached f_stop at %g frames, expected ~%g",
            fs, found, want_frames);
    }

    /* 24 h ceiling: accepted at the bound, refused above it, and the bound
     * must be reachable rather than merely declared -- 24 h of frames has to
     * survive the u = n/T arithmetic at the highest rate. */
    {
        csd_gen_t g;
        ok(arm(g, 48000u, CSD_GEN_SWEEP, 1.0, 20000.0, CSD_GEN_LOG, AMP_DEF,
               CSD_GEN_PERIOD_MAX_MS) == 0, "24 h sweep rejected");
        ok(arm(g, 48000u, CSD_GEN_SWEEP, 1.0, 20000.0, CSD_GEN_LOG, AMP_DEF,
               CSD_GEN_PERIOD_MAX_MS + 1) != 0, "sweep above 24 h accepted");
        csd_gen_t h;
        arm(h, 192000u, CSD_GEN_SWEEP, 1.0, 20000.0, CSD_GEN_LIN, AMP_DEF,
            CSD_GEN_PERIOD_MAX_MS);
        size_t T = (size_t)h.period_frames;
        double f_end = csd_gen_frequency(&h, T) / (double)CSD_GEN_HZ_ONE;
        okf(fabs(f_end - 20000.0) < 1.0, "24 h sweep ended at %g Hz", f_end);
        double f_q = csd_gen_frequency(&h, T / 2) / (double)CSD_GEN_HZ_ONE;
        okf(fabs(f_q - 10000.5) < 20.0, "24 h sweep midpoint %g Hz", f_q);
    }
}

/* ------------------------------------------------------ T5 multitone */

/* Deliberately wrong: sum without per-tone scaling, so the in-phase stack of
 * sines clips. Asserted to fail the clipping test below. */
static void run_multitone_unnormalised(csd_gen_t &g, std::vector<int32_t> &out,
                                       size_t n)
{
    out.resize(n);
    for (size_t i = 0; i < n; i++) {
        int64_t acc = 0;
        for (unsigned k = 0; k < g.tones; k++) {
            g.pfrac[k] += g.mt_step[k] & 0xFFull;
            g.phase[k] += (uint32_t)(g.mt_step[k] >> 8) + (uint32_t)(g.pfrac[k] >> 8);
            g.pfrac[k] &= 0xFFull;
            acc += csd_gen_sine(g.phase[k]);          /* no per-tone division */
        }
        int64_t y = (acc * (int64_t)g.amp) >> 31;
        out[i] = (int32_t)(y > 2147483647ll ? 2147483647ll :
                          (y < -2147483648ll ? -2147483648ll : y));
    }
}

static void t5_multitone(void)
{
    printf("T5 multitone 2..4 tones\n");
    const double sets[3][4] = {
        {100.0, 1000.0, 0, 0},
        {100.0, 1000.0, 5000.0, 0},
        {997.0, 3001.0, 7003.0, 11003.0},
    };
    for (int t = 0; t < 3; t++) {
        unsigned n_tones = t + 2;
        csd_gen_t g;
        csd_gen_reset(&g);
        uint32_t fq[4];
        for (unsigned i = 0; i < n_tones; i++)
            fq[i] = (uint32_t)llround(sets[t][i] * CSD_GEN_HZ_ONE);
        /* Order independence: set the tones before the arm in one case. */
        if (t == 0) {
            ok(csd_gen_tones(&g, fq, n_tones) == 0, "tones(before arm) rejected");
            ok(csd_gen_wave(&g, 48000u, CSD_GEN_MULTITONE, CSD_GEN_HZ_ONE,
                            CSD_GEN_HZ_ONE, CSD_GEN_LIN, AMP_DEF, 0, nullptr) == 0,
               "multitone arm rejected");
        } else {
            ok(csd_gen_wave(&g, 48000u, CSD_GEN_MULTITONE, CSD_GEN_HZ_ONE,
                            CSD_GEN_HZ_ONE, CSD_GEN_LIN, AMP_DEF, 0, nullptr) == 0,
               "multitone arm rejected");
            ok(csd_gen_tones(&g, fq, n_tones) == 0, "tones(after arm) rejected");
        }

        size_t n = coherent_n(1.0, FS_DEF, 4.0);      /* 4 s resolves 0.25 Hz */
        std::vector<int32_t> x;
        run(g, x, n);

        double peak = 0;
        for (unsigned i = 0; i < n_tones; i++) {
            double a = goertzel(x, sets[t][i], FS_DEF);
            if (a > peak) peak = a;
            /* THE SILENCE REGRESSION: mt_step was once left unfilled, so
             * multitone emitted nothing while every other mode passed. */
            okf(a > 0.01, "multitone[%u]: tone %g Hz level %g -- silent output?",
                n_tones, sets[t][i], a);
        }
        for (unsigned i = 0; i < n_tones; i++) {
            double a = goertzel(x, sets[t][i], FS_DEF);
            okf(db(a, peak) > -1.0,
                "multitone[%u]: tone %g Hz is %g dB below peak (want equal)",
                n_tones, sets[t][i], db(a, peak));
        }
        /* A frequency nobody asked for must be empty: catches a tone-count bug
         * that keeps oscillating the previous configuration. */
        double absent = 13456.0;
        okf(db(goertzel(x, absent, FS_DEF), peak) < -95.0,
            "multitone[%u]: unrequested %g Hz at %g dB", n_tones, absent,
            db(goertzel(x, absent, FS_DEF), peak));

        int32_t mx, mn; peak_of(x, mx, mn);
        okf(std::max(mx, -mn) < (int32_t)AMP_DEF,
            "multitone[%u]: peak %d exceeds requested amplitude %u",
            n_tones, std::max(mx, -mn), AMP_DEF);
    }

    /* CANARY: without per-tone scaling the in-phase stack clips, so the
     * amplitude assertion above cannot pass vacuously. */
    {
        csd_gen_t g;
        csd_gen_reset(&g);
        uint32_t fq[4] = {997u << 8, 3001u << 8, 7003u << 8, 11003u << 8};
        csd_gen_wave(&g, 48000u, CSD_GEN_MULTITONE, CSD_GEN_HZ_ONE,
                     CSD_GEN_HZ_ONE, CSD_GEN_LIN, AMP_DEF, 0, nullptr);
        ok(csd_gen_tones(&g, fq, 4) == 0, "canary tones rejected");
        std::vector<int32_t> xn;
        run_multitone_unnormalised(g, xn, 200000);
        int32_t mx, mn; peak_of(xn, mx, mn);
        okf(std::max(mx, -mn) >= (int32_t)AMP_DEF,
            "SELF-CHECK: un-normalised multitone peak %d stayed under %u -- "
            "the clipping assertion has no teeth", std::max(mx, -mn), AMP_DEF);
    }

    /* Count validation: 1 and 5 refused, not clamped. A silent clamp turns a
     * typo'd 7 into an unremarkable 4-tone result. */
    {
        csd_gen_t g;
        csd_gen_reset(&g);
        csd_gen_wave(&g, 48000u, CSD_GEN_MULTITONE, CSD_GEN_HZ_ONE,
                     CSD_GEN_HZ_ONE, CSD_GEN_LIN, AMP_DEF, 0, nullptr);
        uint32_t one[1] = {1000u << 8};
        uint32_t five[5] = {1000u << 8, 2000u << 8, 3000u << 8, 4000u << 8, 5000u << 8};
        ok(csd_gen_tones(&g, one, 1) != 0, "1 tone accepted (want 2..4)");
        ok(csd_gen_tones(&g, five, 5) != 0, "5 tones accepted (want 2..4)");
        ok(csd_gen_tones(&g, five, 4) == 0, "4 tones rejected");
    }
}

/* ----------------------------------------------------------- T6 noise */
static void t6_noise(void)
{
    printf("T6 noise\n");
    csd_gen_t g;
    ok(arm(g, 48000u, CSD_GEN_NOISE, 1000.0, 1000.0, CSD_GEN_LIN, AMP_DEF, 0) == 0,
       "noise arm rejected");
    size_t n = 1u << 19;
    std::vector<int32_t> x;
    run(g, x, n);

    double mean = 0, var = 0;
    for (size_t i = 0; i < n; i++) mean += x[i];
    mean /= (double)n;
    for (size_t i = 0; i < n; i++) { double d = x[i] - mean; var += d * d; }
    var /= (double)n;
    double rms = sqrt(var) / 2147483648.0;
    double theory = ((double)AMP_DEF / 2147483648.0) / sqrt(3.0);
    okf(fabs(rms - theory) / theory < 0.02,
        "noise rms %g, uniform theory %g", rms, theory);
    /* Mean must be ~0. Tolerance derived, not picked: uniform samples with
     * sigma = A/sqrt(3) have a mean whose standard error is sigma/sqrt(N),
     * which at A=0.5 and N=2^19 is 8.1e-4. The previous 2e-4 limit sat INSIDE
     * one standard error, so a correct generator failed it most of the time.
     * Four standard errors is a limit that a real DC offset still breaks. */
    double se = (0.5 / sqrt(3.0)) / sqrt((double)n);
    okf(fabs(mean) / 2147483648.0 < 4.0 * se,
        "noise mean %g (want < %g, 4 sigma of the estimate)",
        mean / 2147483648.0, 4.0 * se);

    /* Flatness by AVERAGED band energy. A single DFT bin of white noise is
     * Rayleigh with ~5 dB standard deviation (measured here: -16 dB to +7 dB),
     * so per-bin assertions either always fail or get loosened into
     * uselessness. Averaging ~200 bins brings the estimate well under a dB and
     * leaves 3 dB as a real limit. */
    const double lo[] = {20, 100, 500, 2000, 8000, 16000};
    const double hi[] = {99, 499, 1999, 7999, 15999, 22000};
    double band[6], grand = 0;
    for (int b = 0; b < 6; b++) {
        double acc = 0; int cnt = 0;
        for (double f = lo[b]; f <= hi[b]; f += 25.0) {
            acc += pow(goertzel(x, f, FS_DEF), 2.0);
            cnt++;
        }
        band[b] = sqrt(acc / (cnt ? cnt : 1));
        grand += band[b];
    }
    grand /= 6.0;
    for (int b = 0; b < 6; b++) {
        okf(fabs(db(band[b], grand)) < 3.0,
            "noise band %g-%g Hz is %g dB from overall (want within 3 dB)",
            lo[b], hi[b], db(band[b], grand));
    }
    /* No shaping: the top band reads the same as the bottom. An accidental
     * recurrence or a filter shows up as a slope. */
    okf(fabs(db(band[0], band[5])) < 3.0,
        "noise low band %g dB from high band (want flat)",
        db(band[0], band[5]));

    /* Period: xorshift32 repeats at 2^32-1, so a capture must not repeat. */
    size_t same = 0, span = 4096;
    for (size_t i = 0; i < span; i++) if (x[i] == x[i + span]) same++;
    okf(same < span / 8, "noise self-repeat over %zu frames: %zu matches",
        span, same);

    /* Different rate and amplitude, checked for DC bias: a constant offset
     * would ride through to the DAC. */
    csd_gen_t h;
    arm(h, 192000u, CSD_GEN_NOISE, 1000.0, 1000.0, CSD_GEN_LIN, 0x60000000u, 0);
    std::vector<int32_t> y;
    run(h, y, 262144);
    double m2 = 0;
    for (size_t i = 0; i < y.size(); i++) m2 += y[i];
    m2 /= (double)y.size();
    double se2 = (0.375 / sqrt(3.0)) / sqrt((double)y.size());
    okf(fabs(m2) / 2147483648.0 < 4.0 * se2, "noise @192k mean %g (want < %g)",
        m2 / 2147483648.0, 4.0 * se2);
}

/* -------------------------------------------------- T7 mode/validation */
static void t7_off_and_validation(void)
{
    printf("T7 OFF silence, argument validation, channel identity\n");
    {
        csd_gen_t g;
        ok(arm(g, 48000u, CSD_GEN_OFF, 1000.0, 20000.0, CSD_GEN_LOG, AMP_DEF, 1000) == 0,
           "OFF arm rejected");
        std::vector<int32_t> x;
        run(g, x, 100000);
        bool all_zero = true;
        for (size_t i = 0; i < x.size(); i++) if (x[i] != 0) all_zero = false;
        ok(all_zero, "OFF emitted non-zero samples -- it must be byte-exact "
                     "silence so the generator can never disturb audio");
    }

    /* Both channels identical: a per-channel difference would be read as DUT
     * channel skew. */
    {
        csd_gen_t g;
        arm(g, 48000u, CSD_GEN_SINE, 997.0, 997.0, CSD_GEN_LIN, AMP_DEF, 0);
        int32_t s[2];
        bool same = true;
        for (int i = 0; i < 5000; i++) {
            csd_gen_sample(&g, s);
            if (s[0] != s[1]) same = false;
        }
        ok(same, "channels differ -- a test source must be mono-identical");
    }

    struct { const char *what; unsigned fs, mode, dom; double fa, fb;
             uint64_t pms; int want_ok; } cases[] = {
        {"fs=0",                  0u,     CSD_GEN_SINE,    CSD_GEN_LIN, 1000, 1000, 0, 0},
        {"mode out of range",     48000u, CSD_GEN_NOISE+9, CSD_GEN_LIN, 1000, 1000, 0, 0},
        {"domain out of range",   48000u, CSD_GEN_SWEEP,   CSD_GEN_LOG+3, 1000, 20000, 1000, 0},
        {"f_start below 1 Hz",    48000u, CSD_GEN_SINE,    CSD_GEN_LIN, 0.003, 1000, 0, 0},
        {"f_start above Nyquist", 48000u, CSD_GEN_SINE,    CSD_GEN_LIN, 24001, 24001, 0, 0},
        {"f_stop above Nyquist",  48000u, CSD_GEN_SWEEP,   CSD_GEN_LIN, 1000, 24001, 1000, 0},
        {"period above 24 h",     48000u, CSD_GEN_SWEEP,   CSD_GEN_LIN, 1000, 20000,
                                        CSD_GEN_PERIOD_MAX_MS + 1, 0},
        {"valid tone",            48000u, CSD_GEN_SINE,    CSD_GEN_LIN, 1000, 1000, 0, 1},
        {"valid square",          48000u, CSD_GEN_SQUARE,  CSD_GEN_LIN, 1000, 1000, 0, 1},
        {"valid log sweep",       48000u, CSD_GEN_SWEEP,   CSD_GEN_LOG, 1, 20000, 30000, 1},
        {"valid noise",           48000u, CSD_GEN_NOISE,   CSD_GEN_LIN, 1000, 1000, 0, 1},
        {"Nyquist exactly",       48000u, CSD_GEN_SINE,    CSD_GEN_LIN, 24000, 24000, 0, 1},
    };
    for (auto &c : cases) {
        csd_gen_t g; csd_gen_reset(&g);
        int why = -99;
        int r = csd_gen_wave(&g, c.fs, c.mode,
                             (uint32_t)llround(c.fa * CSD_GEN_HZ_ONE),
                             (uint32_t)llround(c.fb * CSD_GEN_HZ_ONE),
                             c.dom, AMP_DEF, c.pms, &why);
        bool got = (r == 0);
        okf(got == (bool)c.want_ok, "%s: %s (why=%d)", c.what,
            got ? "accepted, want rejected" : "rejected, want accepted", why);
        if (!c.want_ok) okf(why != 0, "%s: rejected but why was not set", c.what);
    }

    /* A rejected arm leaves the previous configuration intact and audible,
     * not half-written to an illegal state. */
    {
        csd_gen_t g;
        arm(g, 48000u, CSD_GEN_SINE, 1000.0, 1000.0, CSD_GEN_LIN, AMP_DEF, 0);
        int why = 0;
        int r = csd_gen_wave(&g, 48000u, CSD_GEN_SINE, 30000u << 8, 30000u << 8,
                             CSD_GEN_LIN, AMP_DEF, 0, &why);
        ok(r != 0, "illegal re-arm accepted");
        ok(g.f_a == (uint32_t)(1000u << CSD_GEN_HZ_SHIFT),
           "illegal re-arm corrupted the descriptor");
        std::vector<int32_t> x;
        run(g, x, 24000);
        double z = zc_hz(x, 0, x.size(), 48000.0);
        double q = 48000.0 / (2.0 * (double)x.size());
        okf(fabs(z - 1000.0) <= 1.5 * q,
            "after rejected re-arm: %g Hz (want 1000, quantum %g)", z, q);
    }

    /* fs=0 fails loudly instead of dividing by zero. */
    {
        csd_gen_t g; csd_gen_reset(&g);
        int why = -1;
        ok(csd_gen_wave(&g, 0u, CSD_GEN_SINE, 1000u << 8, 1000u << 8,
                        CSD_GEN_LIN, AMP_DEF, 0, &why) != 0, "fs=0 accepted");
        g.mode = CSD_GEN_SINE; g.fs = 0u;
        g.f_a = g.f_b = 1000u << CSD_GEN_HZ_SHIFT;
        int32_t s[2] = {1, 1};
        csd_gen_sample(&g, s);
        ok(true, "sample path survived fs=0");
    }

    /* Re-arming restarts: the frame index IS the time argument of the sweep
     * formula, so a second identical command must not resume mid-sweep. */
    {
        csd_gen_t g;
        arm(g, 48000u, CSD_GEN_SWEEP, 1.0, 20000.0, CSD_GEN_LIN, AMP_DEF, 10000);
        std::vector<int32_t> junk;
        run(g, junk, 48000 * 8);                  /* 8 s into a 10 s sweep */
        double before = csd_gen_frequency(&g, g.n) / (double)CSD_GEN_HZ_ONE;
        okf(before > 15000.0, "sweep did not advance (frequency %g)", before);
        arm(g, 48000u, CSD_GEN_SWEEP, 1.0, 20000.0, CSD_GEN_LIN, AMP_DEF, 10000);
        double again = csd_gen_frequency(&g, 0) / (double)CSD_GEN_HZ_ONE;
        okf(fabs(again - 1.0) < 1.0 / CSD_GEN_HZ_ONE,
            "re-arm resumed at %g Hz instead of restarting", again);
    }
}

int main(void)
{
    printf("=== csd_gen host gate ===\n");
    t1_sine();
    t2_freq_accuracy();
    t3_square();
    t4_sweep();
    t5_multitone();
    t6_noise();
    t7_off_and_validation();
    printf("\n%d checks, %d failures\n", checks, fails);
    printf(fails ? "GEN HOST GATE: FAIL\n" : "GEN HOST GATE: PASS\n");
    return fails ? 1 : 0;
}
