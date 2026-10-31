// CSD function generator: the wave is a FUNCTION of time, not a sequence of
// per-frame updates.
//
// csd_gen_wave(g, mode, f_start, f_stop, domain, amp, period_ms) installs one
// descriptor. csd_gen_sample(g, samp) returns the next frame, taking the
// instantaneous frequency from its CLOSED FORM at the current frame index:
//
//     linear:  f(t) = f_a + (f_b - f_a) * (t/T)
//     log:     f(t) = f_a * (f_b/f_a)^(t/T)  ==  2^(ea + eb*t/T)
//
// Direction is not a case. f_start > f_stop, f_start < f_stop and
// f_start == f_stop all fall out of the same two lines -- there is no ascending
// and descending path to keep in sync, and the equal case degenerates to a
// steady tone without a special branch, so a tone and a sweep cannot drift
// apart in behaviour the way two implementations would.
//
// WHY NOT STEP THE FREQUENCY FORWARD
// The first implementation did: step *= ratio each frame. For a slow log sweep
// the product rounded back to its own operand -- step*1.00002 == step in
// integer arithmetic -- so the tone never accelerated and a 30 s log sweep
// finished at 1 Hz instead of 20 kHz. Deriving f(t) from the formula deletes
// that failure mode instead of tuning around it, and it is what makes the
// sweep a formula rather than a list of precomputed packets. Only the PHASE
// accumulates, which is unavoidable and exact: phase is the integral of
// frequency, so it wraps at 2^32 by definition and cannot drift in frequency.
//
// WHY EVERYTHING IS INTEGER
// XS3 has no floating-point unit, so a float op is a soft-float call, and this
// runs once per frame on the audiohub core inside UserBufferManagement. The
// only transcendental on the per-frame path is 2^x for the log sweep, from a
// tabulated mantissa plus an integer exponent shift. Its two coefficients are
// computed once, in double precision, when the sweep is armed.
//
// WHY THE SQUARE IS A HARMONIC SUM
// A hard-switched square has harmonics to infinity; sampled, everything above
// Nyquist folds back as in-band spurs, so a DUT would be measured against
// aliased artefacts that look like its own behaviour. The usual fix is a
// polyBLEP residual. It was tried here and rejected by measurement, not taste:
// the order-2 residual came out WORSE than doing nothing (folded 3rd harmonic
// at -4 dB versus -9 dB naive, even harmonics at -20 dB where a square has
// none). The convention is easy to get subtly wrong and impossible to eyeball,
// because a broken polyBLEP still looks like a square on a scope.
//
// So the square is the odd-harmonic sum, each term its own NCO sine. Every
// oscillator is strictly below Nyquist -- terms past it are DROPPED, never
// folded -- so nothing can alias, and that holds by construction rather than
// depending on a polynomial.
//
// ACCEPTED LIMITATION (user-directed 2026-10-31): low-frequency fidelity of
// the square is not a requirement. Band-limiting to the 63rd odd harmonic
// leaves a few-Hz square with rounded edges.
#ifndef __csd_gen_core_h__
#define __csd_gen_core_h__

#include <stdint.h>
#include <string.h>
#include "csd_gen_tables.h"

#define CSD_GEN_MAX_TONES    4
#define CSD_GEN_MIN_TONES    2

/* Odd harmonics 1,3,..,2N-1. 32 terms reach the 63rd, which is the complete
 * in-band series above 762 Hz at 48 kHz. ~10 XS3 cycles per term (table read,
 * interpolate, multiply, accumulate) => ~320 of the audio core's ~12,500
 * cycle per-frame budget at 48 kHz. */
#define CSD_GEN_SQ_HARMONICS 32

/* Frequency is Hz in Q8: 1/256 Hz resolution, and 96000 Hz at 192 kHz sample
 * rate needs 2.46e7, which fits. Q16 would NOT: 96000 << 16 = 6.3e9 overflows
 * uint32, so a "nicer" shift silently breaks the top two sample rates. */
#define CSD_GEN_HZ_SHIFT 8
#define CSD_GEN_HZ_ONE   (1u << CSD_GEN_HZ_SHIFT)

/* Upper bound on sweep duration. Unbounded was the first request and is
*  deliberately capped at 24 h: the arithmetic below is sized for it, and a
 * longer sweep is not a measurement anyone is making. 24 h at 192 kHz is
 * 1.66e10 frames -- still 1000x beyond the 30 s this was specified for. */
#define CSD_GEN_PERIOD_MAX_MS (24ull * 3600ull * 1000ull)

typedef enum {
    CSD_GEN_OFF = 0,
    CSD_GEN_SINE = 1,
    CSD_GEN_SQUARE = 2,
    CSD_GEN_SWEEP = 3,          /* linear or log per `domain` */
    CSD_GEN_MULTITONE = 4,
    CSD_GEN_NOISE = 5
} csd_gen_mode_t;

typedef enum {
    CSD_GEN_LIN = 0,
    CSD_GEN_LOG = 1
} csd_gen_domain_t;

typedef struct {
    unsigned mode;
    uint32_t amp;                      /* Q31 peak */
    unsigned domain;                   /* csd_gen_domain_t */

    /* The descriptor: the whole definition of the wave. fs is part of it --
     * see csd_gen_wave, which takes the sample rate as an argument rather
     * than relying on someone having set it earlier. */
    unsigned fs;                       /* frames per second */
    uint32_t f_a;                      /* Q8 Hz, start */
    uint32_t f_b;                      /* Q8 Hz, stop (== f_a for a tone) */
    uint64_t period_ms;                /* requested duration, kept verbatim */
    uint64_t period_frames;            /* 0 => hold f_a indefinitely */
    int64_t  ea, eb;                   /* log sweep exponents, Q24 */

    uint32_t mt_f[CSD_GEN_MAX_TONES];  /* Q8 Hz per tone */
    unsigned tones;

    /* Phase is the ONLY carried waveform state: the integral of f(t).
     * Sized for the square, which needs one oscillator per harmonic -- 32 of
     * them. Reusing a smaller ring with `k % N` would make harmonic k and
     * harmonic k+N share one accumulator driven by two different steps, which
     * is not a rounding error, it is a different waveform. Multitone uses the
     * first CSD_GEN_MAX_TONES slots. */
    uint32_t phase[CSD_GEN_SQ_HARMONICS];
    uint64_t pfrac[CSD_GEN_SQ_HARMONICS]; /* sub-LSB of the step, so a 1 Hz
                                           * tone at 192 kHz is not biased flat */
    uint64_t n;                        /* frames emitted since arm */
    uint32_t rng;                      /* noise state, never zero */

    /* Multitone steps, precomputed at arm. The tones never change while
     * running, so dividing four frequencies per frame would be four software
     * divides (XS3 has no hardware divider) to recompute a constant. */
    uint64_t mt_step[CSD_GEN_MAX_TONES];
} csd_gen_t;

/* ------------------------------------------------------------ fixed-point */

static int32_t csd_gen_sat64(int64_t v)
{
    if (v > 2147483647ll)  return 2147483647;
    if (v < -2147483648ll) return -2147483648;
    return (int32_t)v;
}

/* Sum a test tone into a live host sample for the PRE-MIXER injection point.
 *
 * Saturation is mandatory, not cosmetic. A plain add wraps when host peak +
 * tone exceed full scale, and a wrap is a full-scale step -- a click. The rig
 * cannot catch it: the tone still measures present at the expected frequency,
 * because the wrap only disturbs the few frames where the host happens to be
 * near peak. A silent, rare, audible defect is exactly the class this gate
 * exists for (see the naive-square canary), so the clamp is tested directly
 * rather than assumed -- T8 in tools/dsp/test_gen.cpp.
 *
 * Reuses csd_gen_sat64() rather than open-coding a clamp: it is the
 * already-tested saturator, and duplicating the limits invites the two
 * copies to drift. The int64 add is deliberate, not accidental sloppiness --
 * it is what makes the add itself incapable of wrapping, and a 32+32+carry
 * pair is cheap on XS3 where a divide is not.
 *
 * A pre-clamp on the operands (limit each to +-3/4 full scale) was the first
 * attempt and is wrong: it clips a legitimate near-full-scale host sample
 * even when the tone is silent, silently costing a dB of real audio. The
 * full-scale-plus-zero pass-through case is asserted in T8 to keep that
 * regression from coming back.
 */
static int32_t csd_gen_mix_sum(int32_t host, int32_t tone)
{
    return csd_gen_sat64((int64_t)host + (int64_t)tone);
}

/* Per-source gain for the pre-mixer injection point (Q15: 32768 = unity).
 *
 * The user requirement: host stream and generator get INDIVIDUAL volume
 * control in the mixer graph, not through the codec register, so each
 * source is scaled by its own weight before the sum. Two constraints the
 * XS3 imposes, both verified in T9 rather than argued:
 *   - no FPU: one int64 multiply and one shift per sample, no divide;
 *   - unity must be BYTE-EXACT. (x * 32768) >> 15 is exactly x for every
 *     x, including INT32_MIN, so the default state costs zero bits of the
 *     host stream -- a (x >> 15) << 15 shortcut would silently eat the low
 *     15 bits and nobody would hear it until a null test did.
 * Rounding is +0.5-biased then floored, so negative odd magnitudes round
 * toward zero -- asserted, not accidental, so a future "cleanup" cannot
 * change audible behaviour unnoticed.
 * w above 32768 is legal (a test source may be boosted) and saturates like
 * the sum: a wrap here is the same click class T8 was written for. */
static int32_t csd_gen_mix_scale(int32_t x, unsigned w_q15)
{
    return csd_gen_sat64(((int64_t)x * (int64_t)w_q15 + 16384) >> 15);
}

/* Scale both pre-mixer sources by their own weights, then sum. This is
 * the whole per-source volume control: w_host moves the host stream,
 * w_gen moves the generator, independently, upstream of doMix. Byte-exact
 * when both weights are unity, which is the state the mixer starts in --
 * verified in T9, because 'default state must not touch the audio' is the
 * claim most likely to be silently false. */
static int32_t csd_gen_mix_gain_sum(int32_t host, int32_t tone,
                                    unsigned w_host, unsigned w_gen)
{
    if (w_host == 32768u && w_gen == 32768u) {
        return csd_gen_mix_sum(host, tone);   /* the proven hot path */
    }
    return csd_gen_mix_sum(csd_gen_mix_scale(host, w_host),
                           csd_gen_mix_scale(tone, w_gen));
}

/* Interpolated sine; phase wraps at 2^32 == 1.0. */
static int32_t csd_gen_sine(uint32_t phase)
{
    unsigned idx = (unsigned)(phase >> 22) & (CSD_GEN_SINE_N - 1u);
    unsigned frac = (unsigned)(phase & 0x3FFFFFu);
    int64_t a = csd_gen_sine_q31[idx];
    int64_t b = csd_gen_sine_q31[(idx + 1u) & (CSD_GEN_SINE_N - 1u)];
    return (int32_t)(a + (((b - a) * (int64_t)frac) >> 22));
}

/* 2^x for x >= 0, where x is log2 of a frequency ALREADY in Q8 -- so the
 * result is that frequency in Q8 Hz, no extra scaling step.
 *
 * THE SHIFT MUST BE >> 29. Reason, because getting it wrong is silent and
 * scales every log-sweep frequency by 256: the mantissa is Q29 and represents
 * the fraction in [1,2), so 2^x = mant * 2^ip / 2^29. Dropping the /2^29 (for
 * instance by "converting Q29 to Q8" with >> 21, which reads convincingly)
 * leaves the answer 256x high -- a 20 kHz sweep endpoint would report 5 MHz,
 * or, after the caller's Nyquist clamp, sit pinned at full scale. Caught by
 * hand-deriving 2^8 with x = log2(1.0 Hz in Q8) = 8, which must return 256.
 *
 * Precision: scaling before the shift keeps all 30 mantissa bits, so the only
 * limit is the table's own interpolation error (measured 1.9e-6 relative,
 * 0.03 Hz worst case over 1..24 kHz). Reversing the order would truncate to 9
 * bits and make the endpoint wrong by hundreds of Hz.
 *
 * NO WRAP-AROUND ON THE SECOND NODE. The table holds EXP_N+1 entries so [i+1]
 * is valid at i=255; masking with (EXP_N-1) there wrapped the last interval
 * back onto entry 0, which sent a 4093 Hz tone to 2654 Hz while leaving every
 * exact power of two perfect -- so a check on round frequencies alone passes.
 * tools/dsp/gen_gen_tables.py now proves this round trip over the whole range.
 *
 * ip is clamped far below what the descriptor can request -- f <= fs/2 bounds
 * x at log2(96000*256) < 25 -- so the clamp only fires on corrupt state. */
static uint32_t csd_gen_exp2_q24(int64_t x_q24)
{
    if (x_q24 <= 0) return (uint32_t)CSD_GEN_HZ_ONE;   /* 2^0 = 1.0 Hz in Q8 */
    int64_t ip = x_q24 >> 24;
    uint32_t frac = (uint32_t)(x_q24 & 0xFFFFFF);
    if (ip > 24) ip = 24;
    unsigned i = frac >> 16;                           /* 0..255 */
    uint32_t sub = frac & 0xFFFFu;
    int64_t m0 = csd_gen_exp2_mant[i];
    int64_t m1 = csd_gen_exp2_mant[i + 1u];            /* inclusive table */
    int64_t mant = m0 + (((m1 - m0) * (int64_t)sub) >> 16);   /* Q29, in [1,2) */
    int64_t r = ((mant << ip) + (1 << 28)) >> 29;      /* round, do not truncate */
    if (r > 0x7FFFFFFFll) r = 0x7FFFFFFFll;            /* caller clamps Nyquist */
    return (uint32_t)r;
}

/* log2(v) in Q24 for v >= 1.0 (Q8). Arm-time only, so the bit scan may loop.
 * Same inclusive-table rule as csd_gen_exp2_q24: [i+1] must not wrap. */
static int64_t csd_gen_log2_q24(uint32_t v)
{
    unsigned p = 0;
    uint32_t t = v;
    while (t > 1u) { t >>= 1; p++; }              /* p = floor(log2 v) */
    if (p == 0u) return 0;
    /* 23 fraction bits below the implicit leading one. */
    uint32_t shifted = (uint32_t)(v << (31 - p));
    uint32_t frac = (shifted >> 8) & 0x7FFFFFu;
    unsigned i = frac >> 15;                      /* 0..255 */
    uint32_t sub = frac & 0x7FFFu;
    int64_t m0 = csd_gen_log2_tab[i];
    int64_t m1 = csd_gen_log2_tab[i + 1u];        /* inclusive table */
    int64_t lf = m0 + (((m1 - m0) * (int64_t)sub) >> 15);      /* Q29 */
    return ((int64_t)p << 24) + (lf >> 5);        /* Q29 -> Q24 */
}

/* Normalised sweep position u = n/T in Q16, clamped to 1.0.
 *
 * Formed as (n << 16)/T rather than (T-reciprocal multiply) because n <= T:
 * at the 24 h ceiling, n << 16 = 1.09e15, which fits int64 with 4 orders of
 * margin. A naive (f_b-f_a)*n would NOT fit -- 1.6e9 * 1.7e10 overflows
 * int64 -- so the ratio is reduced first and the frequency span applied
 * afterwards. That ordering is the whole reason this helper exists. */
static uint32_t csd_gen_u_q16(uint64_t n, uint64_t T)
{
    if (T == 0ull) return 0u;
    if (n >= T) return 1u << 16;
    return (uint32_t)((n << 16) / T);
}

/* THE SWEEP FORMULA. Direction-free: f_b > f_a rises, f_b < f_a falls,
 * f_b == f_a holds -- all three from these lines, no mode flag for direction
 * and so no possibility of the three cases disagreeing. */
static uint32_t csd_gen_frequency(const csd_gen_t *g, uint64_t n)
{
    if (g->period_frames == 0ull || g->f_a == g->f_b) return g->f_a;

    uint32_t u = csd_gen_u_q16(n, g->period_frames);

    if (g->domain == CSD_GEN_LIN) {
        int64_t span = (int64_t)g->f_b - (int64_t)g->f_a;      /* signed: falls */
        int64_t f = (int64_t)g->f_a + ((span * (int64_t)u) >> 16);
        if (f < (int64_t)CSD_GEN_HZ_ONE) f = (int64_t)CSD_GEN_HZ_ONE;
        return (uint32_t)f;
    }
    /* log: f = 2^(ea + eb*u). Constant RATIO per unit time, which is what
     * makes a log sweep log and not a badly-shaped linear one. */
    int64_t x = g->ea + ((g->eb * (int64_t)u) >> 16);
    return csd_gen_exp2_q24(x);
}

/* Phase increment for a Q8 Hz frequency at fs, in Q8-of-a-step, so the
 * fraction below one phase LSB survives in pfrac instead of biasing the
 * produced frequency flat.
 *
 * THIS IS THE ONLY DIVIDE ON THE PER-FRAME PATH BESIDES csd_gen_u_q16, and it
 * is called ONCE per frame, never per oscillator. That matters because XS3 has
 * no hardware divider: a 64-bit divide is a software routine of order 100
 * cycles, so 32 of them per frame (one per square harmonic) would consume a
 * third of the audio core's ~12,500 cycle budget for nothing. The phase step is
 * LINEAR in frequency -- step((2k+1)f) == (2k+1)*step(f) -- so the harmonic
 * steps are exact integer multiples of this one value. Multiplying a divided
 * fundamental is both cheaper and more accurate than dividing 32 times,
 * because it cannot accumulate 32 independent roundings.
 *
 * A reciprocal multiply was the obvious alternative and is wrong here: with
 * inv_fs = floor(2^48/fs) the truncation costs up to 1.5 phase LSBs per frame,
 * a ~1.6 Hz error on every tone, which for a test source is the entire point. */
static uint64_t csd_gen_pstep(uint32_t f_q8, unsigned fs)
{
    if (fs == 0u) return 0ull;
    return (((uint64_t)f_q8 << 32) + (fs / 2u)) / fs;   /* rounds, not floors */
}

/* Advance oscillator `i` by a precomputed step. */
static void csd_gen_advance(csd_gen_t *g, unsigned i, uint64_t ps)
{
    g->pfrac[i] += ps & 0xFFull;
    uint32_t inc = (uint32_t)(ps >> 8) + (uint32_t)(g->pfrac[i] >> 8);
    g->pfrac[i] &= 0xFFull;
    g->phase[i] += inc;
}

/* ------------------------------------------------------------------- arm */

static void csd_gen_reset(csd_gen_t *g)
{
    memset(g, 0, sizeof *g);
    g->amp = 0x40000000u;              /* -6 dBFS */
    g->tones = CSD_GEN_MIN_TONES;
    g->rng = 0x9E3779B9u;              /* xorshift must never see zero */
    g->domain = CSD_GEN_LIN;
}

/* Derive every per-rate quantity from the stored descriptor. THE ONLY place
 * that turns (fs, descriptor) into frame-domain state, so csd_gen_wave and
 * csd_gen_set_rate cannot drift apart -- two copies of this arithmetic is how
 * a rate change ends up playing a sweep of the wrong length.
 *
 * Called by both entry points; never call it directly from the audio path. */
static int csd_gen_apply_fs(csd_gen_t *g)
{
    if (g->fs == 0u) return -1;
    if (g->period_ms > CSD_GEN_PERIOD_MAX_MS) return -1;

    /* xorshift(0) is 0 forever, so an rng that reached zero -- an all-zero
     * static that never saw csd_gen_reset, a corrupted copy -- makes noise
     * mode silent with no other symptom anywhere in the build. Re-seed here:
     * this runs once per arm/rate-change, never per frame. */
    if (g->rng == 0u) g->rng = 0x9E3779B9u;

    uint32_t fmax = ((uint32_t)g->fs >> 1) << CSD_GEN_HZ_SHIFT;
    if (g->f_a < CSD_GEN_HZ_ONE || g->f_a > fmax) return -1;
    if (g->f_b < CSD_GEN_HZ_ONE || g->f_b > fmax) return -1;

    /* +500 rounds to the nearest frame instead of truncating a 500 ms sweep
     * to 499.99 ms. */
    g->period_frames = g->period_ms
        ? (g->period_ms * (uint64_t)g->fs + 500ull) / 1000ull
        : 0ull;

    if (g->mode == CSD_GEN_SWEEP && g->domain == CSD_GEN_LOG && g->period_frames) {
        /* Computed ONCE per arm/rate-change, on the calling core. eb carries
         * its own sign, so a falling sweep is the same code as a rising one. */
        g->ea = csd_gen_log2_q24(g->f_a);
        g->eb = csd_gen_log2_q24(g->f_b) - g->ea;
    } else {
        g->ea = 0; g->eb = 0;
    }

    /* Multitone steps: frequencies never change while running, so dividing
     * them once here keeps the per-frame path free of software divides (XS3
     * has no hardware divider). */
    for (unsigned i = 0; i < CSD_GEN_MAX_TONES; i++) {
        g->mt_step[i] = g->mt_f[i] ? csd_gen_pstep(g->mt_f[i], g->fs) : 0ull;
        if (g->mt_f[i] && (g->mt_f[i] < CSD_GEN_HZ_ONE || g->mt_f[i] > fmax))
            return -1;
    }
    return 0;
}

/* Re-arm after the stream sample rate changed. The descriptor is unchanged --
 * "1 Hz to 20 kHz, 30 s, log" means the same thing at 44.1 kHz and at 192 kHz
 * -- so only the frame-domain derivations are rebuilt. Phase restarts: a
 * mid-sweep rate change has no well-defined continuation, and restarting the
 * sweep is honest where carrying on would silently stretch or compress it.
 *
 * Returns -1 if the new rate makes the descriptor illegal (e.g. 20 kHz is no
 * longer below Nyquist); the caller should then leave the generator OFF rather
 * than keep emitting a wave that exceeds the new Nyquist limit. */
static int csd_gen_set_rate(csd_gen_t *g, unsigned fs)
{
    if (fs == 0u) return -1;
    unsigned save = g->fs;
    g->fs = fs;
    if (csd_gen_apply_fs(g) != 0) { g->fs = save; return -1; }
    g->n = 0ull;
    for (unsigned i = 0; i < CSD_GEN_SQ_HARMONICS; i++) {
        g->phase[i] = 0u;
        g->pfrac[i] = 0ull;
    }
    return 0;
}

/* ONE ENTRY POINT. The whole wave is described by
 *
 *     (fs, mode, f_start, f_stop, domain, amplitude, period)
 *
 * plus the tone list for multitone. fs is an argument, not ambient state: the
 * generator used to read g->fs, which reset() zeroes, so arming before the
 * audio stream had reported its rate failed with an unexplained error and the
 * caller had to know to set the field first. Nyquist can only be checked
 * against a rate, so the rate is part of the request.
 *
 * Returns 0, or -1 with *why set to the first failed check so the SCPI layer
 * can report a real error instead of silently staying quiet. */
static int csd_gen_wave(csd_gen_t *g, unsigned fs, unsigned mode,
                        uint32_t f_start_q8, uint32_t f_stop_q8,
                        unsigned domain, uint32_t amp,
                        uint64_t period_ms, int *why)
{
    if (why) *why = 1;
    if (fs == 0u) return -1;
    if (why) *why = 2;
    if (mode > CSD_GEN_NOISE) return -1;
    if (why) *why = 3;
    if (mode == CSD_GEN_SWEEP && domain > CSD_GEN_LOG) return -1;
    if (why) *why = 4;
    if (f_start_q8 < CSD_GEN_HZ_ONE) return -1;            /* 1 Hz floor */
    if (why) *why = 5;
    if (period_ms > CSD_GEN_PERIOD_MAX_MS) return -1;

    /* Staged locally: if any derivation below rejects the descriptor, the
     * running generator keeps its previous configuration instead of being
     * half-updated to an illegal one. */
    unsigned save_fs = g->fs;
    uint32_t save_a = g->f_a, save_b = g->f_b, save_amp = g->amp;
    unsigned save_mode = g->mode, save_domain = g->domain;
    uint64_t save_ms = g->period_ms;

    g->fs = fs;
    g->mode = mode;
    g->f_a = f_start_q8;
    g->f_b = (f_stop_q8 < CSD_GEN_HZ_ONE) ? f_start_q8 : f_stop_q8;
    g->domain = domain;
    g->amp = amp;
    g->period_ms = period_ms;

    if (why) *why = 6;
    if (csd_gen_apply_fs(g) != 0) {
        g->fs = save_fs; g->f_a = save_a; g->f_b = save_b;
        g->amp = save_amp; g->mode = save_mode;
        g->domain = save_domain; g->period_ms = save_ms;
        return -1;
    }

    /* Fresh start: the frame index IS the time argument of the sweep
     * formula, so re-arming must rewind it or a second identical command
     * would resume mid-sweep. */
    g->n = 0ull;
    for (unsigned i = 0; i < CSD_GEN_SQ_HARMONICS; i++) {
        g->phase[i] = 0u;
        g->pfrac[i] = 0ull;
    }
    if (why) *why = 0;
    return 0;
}

/* Explicit tone list, 2..4 frequencies. Count out of range is rejected rather
 * than clamped: a silent clamp turns a typo'd 7 into an unremarkable 4-tone
 * result.
 *
 * Stores frequencies only. csd_gen_apply_fs() turns them into phase steps, so
 * this may be called before or after csd_gen_wave without depending on which
 * one knows the sample rate -- an ordering dependency here previously meant
 * the sample path read steps that nothing had filled, and multitone was
 * silent while every other mode passed. */
static int csd_gen_tones(csd_gen_t *g, const uint32_t *f_q8, unsigned count)
{
    if (count < CSD_GEN_MIN_TONES || count > CSD_GEN_MAX_TONES) return -1;
    for (unsigned i = 0; i < count; i++)
        if (f_q8[i] < CSD_GEN_HZ_ONE) return -1;
    g->tones = count;
    for (unsigned i = 0; i < count; i++) g->mt_f[i] = f_q8[i];
    for (unsigned i = count; i < CSD_GEN_MAX_TONES; i++) g->mt_f[i] = 0u;
    /* Re-derive only when a complete descriptor already exists (i.e. tones is
     * being edited after arming). Called before csd_gen_wave, f_a is still
     * zero and apply_fs would reject the half-built descriptor; csd_gen_wave
     * derives the steps itself in that order. */
    if (g->fs && g->f_a >= CSD_GEN_HZ_ONE) return csd_gen_apply_fs(g);
    return 0;
}

/* --------------------------------------------------------------- output */

/* Normalisation is indexed by the number of terms that actually fit under
 * Nyquist, NOT fixed at the 32-term value. The partial sum of a band-limited
 * square overshoots by an N-dependent Gibbs amount -- 1.00000 for one term
 * (it is then a pure sine), 0.94281 for two, 0.92603 for 32 -- so a single
 * constant makes `amp` mean the requested amplitude at one frequency and
 * something else everywhere else. Measured: a 1 kHz square (24 terms at
 * 48 kHz) peaked ~0.04% over the requested amplitude under the fixed
 * constant, which the host gate now refuses. */

static void csd_gen_sample(csd_gen_t *g, int32_t samp[2])
{
    int64_t y;

    switch (g->mode) {
    case CSD_GEN_SINE:
    case CSD_GEN_SWEEP: {
        uint32_t fq = csd_gen_frequency(g, g->n);
        csd_gen_advance(g, 0, csd_gen_pstep(fq, g->fs));
        y = ((int64_t)csd_gen_sine(g->phase[0]) * (int64_t)g->amp) >> 31;
        break;
    }

    case CSD_GEN_SQUARE: {
        uint32_t f1 = csd_gen_frequency(g, g->n);
        uint32_t nyq_q8 = ((uint32_t)g->fs >> 1) << CSD_GEN_HZ_SHIFT;
        /* ONE divide for all 32 oscillators. step((2k+1)*f) == (2k+1)*step(f)
         * exactly, because the phase step is linear in frequency, so scaling
         * the divided fundamental is not an approximation of dividing each
         * harmonic -- it is the same value, with one rounding instead of 32. */
        uint64_t base = csd_gen_pstep(f1, g->fs);
        int64_t acc = 0;
        unsigned used = 0;
        for (unsigned k = 0; k < CSD_GEN_SQ_HARMONICS; k++) {
            unsigned h = 2u * k + 1u;
            /* Dropped, never folded. This comparison is the whole reason the
             * square cannot alias; a polyBLEP tries to cancel the fold after
             * it happens, which is what failed here. */
            if ((uint64_t)f1 * h > nyq_q8) break;
            csd_gen_advance(g, k, base * (uint64_t)h);
            acc += ((int64_t)csd_gen_sine(g->phase[k]) *
                    (int64_t)(2147483647ll / (int64_t)h)) >> 31;
            used++;
        }
        if (used == 0u) { samp[0] = 0; samp[1] = 0; g->n++; return; }
        /* Gibbs normalisation by the term count actually used, so `amp` is
         * the delivered peak at every frequency. csd_gen_gibbs_q30[N] is
         * 1/peak(N) in CSD_GEN_GIBBS_SHIFT -- both the name and the shift
         * come from the generated header, so a scale change in the generator
         * cannot leave this site silently 6 dB off. */
        y = (((acc * (int64_t)csd_gen_gibbs_q30[used]) >> CSD_GEN_GIBBS_SHIFT)
             * (int64_t)g->amp) >> 31;
        break;
    }

    case CSD_GEN_MULTITONE: {
        unsigned n = g->tones;
        int64_t acc = 0;
        if (n > CSD_GEN_MAX_TONES) n = CSD_GEN_MAX_TONES;
        for (unsigned i = 0; i < n; i++) {
            csd_gen_advance(g, i, g->mt_step[i]);
            acc += csd_gen_sine(g->phase[i]);
        }
        /* Equal per-tone amplitude keeps the sum inside full scale for every
         * relative alignment, including the fully in-phase one. */
        y = ((acc * (int64_t)g->amp) >> 31) / (int64_t)(n ? n : 1);
        break;
    }

    case CSD_GEN_NOISE: {
        uint32_t x = g->rng;
        x ^= x << 13; x ^= x >> 17; x ^= x << 5;
        g->rng = x;
        y = ((int64_t)(int32_t)x * (int64_t)g->amp) >> 31;
        break;
    }

    case CSD_GEN_OFF:
    default:
        samp[0] = 0; samp[1] = 0;
        g->n++;
        return;
    }

    g->n++;                       /* the frame index IS the time argument */
    int32_t v = csd_gen_sat64(y);
    samp[0] = v;
    samp[1] = v;                   /* both channels identical: a per-channel
                                   * difference would read as DUT skew */
}

#endif /* __csd_gen_core_h__ */
