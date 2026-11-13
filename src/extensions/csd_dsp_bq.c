// CSD PCM DSP kernel store + scalar engines -- see csd_dsp.h for the contract.
//
// SHIPPED DEFAULT kernel (2026-11-10 owner design change): the identity
// biquad (b0 = 2^30, rest 0). PRESet >= 2 is transparent out of the box;
// kernels (the former bass shelf, FIRs, anything the tester composes) are
// host-loaded as BASE64 KERNEL CONTAINERS over DIAG:DSP:COEFFicient -- the
// block write is the ONLY write path (owner directives 2026-11-10: no
// per-index decimal set, no five-comma form; a half-loaded filter is a
// refused filter).
//
// Why scalar engines and not the fd block FIR: the block engine is
// arithmetically correct (vendor fd_block_fir test passes on this XU316) but
// it works in bursts -- measured ~11,900 reference-time ticks per 128-sample
// block per channel. The audiohub thread must hand a frame to the I2S ports
// every LRCLK period (about 1560 ticks at 384 kHz): HandleSampleClock() sets
// syncError, the "while(!syncError)" loop in xua_audiohub.xc leaves the
// streaming loop, and the sample-exchange -- and with it the audio -- stops.
// A block algorithm's burst can never fit a per-frame deadline, so blocking
// DSP does not belong on the bit-timed thread (AN02011/AN02014 exist for
// that reason). Both engines here run on the EQ TASK core (csd_dsp_eq_task,
// tile[1]), where a burst competes with no LRCLK: the bit-timed loop only
// ever pays the O(1) ring hand-off (csd_eq_ring.c).
//
// Coefficient layout per the API contract: [b0/a0, b1/a0, b2/a0, -a1/a0,
// -a2/a0] in Q(1+lsh).30, state double-word aligned. The FIR target is
// plain Q1.30 direct-form convolution, taps <= CSD_KERNEL_FIR_TAPS.
#include <string.h>
#include <stdint.h>
#include "dsp/adsp.h"
#include "csd_dsp.h"
#include "csd_dsp_bq_coeffs.h"
#include "csd_eq_ring.h"   /* csd_eq_cfg_idx_get: rate stamp for kernel posts */

#ifdef CSD_DSP_DEBUG
#include <stdio.h>
#endif

static int32_t bq_state[2][8] __attribute__((aligned(8)));   // per channel
static const int32_t *bq_c = 0;     /* active biquad coeffs (task-owned)     */
static int32_t bq_lsh = 0;
static int bq_ready = 0;

static unsigned k_engine = CSD_KERNEL_ID_BIQUAD;   /* task-owned after config */
static unsigned k_live_taps = 5;
/* The three big stores live in csd_dsp_store.c, in the tail-most DP bucket
 * (.dp.bss.large) so their size can never shift the bit-timed loop's data
 * layout -- see the block comment there. Task-owned: k_live/fir_hist. */
extern int32_t  csd_k_live[CSD_KERNEL_MAX_TAPS];
extern int32_t  csd_fir_hist[2][CSD_KERNEL_MAX_TAPS];
#define k_live   csd_k_live
#define fir_hist csd_fir_hist
static unsigned fir_head = 0;                      /* task-owned circular idx */

/* Host kernel aperture (2026-11-10, owner design changes). The SHIPPED
 * DEFAULT kernel is the unity/identity biquad: b0 = 2^30 in Q1.30 (lsh 0),
 * everything else zero -- the accumulator is (2^30 * x) >> 30 = x exactly
 * for any int32 input: no rounding freedom, no state feedback, no
 * saturation. Preset >= 2 is therefore TRANSPARENT out of the box and the
 * product applies no EQ by default (the former bass shelf is now just one
 * kernel a host may load -- tools/dsp/gen_biquad.py keeps its design and
 * gate and prints container commands with `--shelf <fs>`).
 *
 * THE BLOCK WRITE IS ATOMIC BY CONSTRUCTION. The whole kernel (header
 * fields + every coefficient word) is one publication: payload words first,
 * k_seq last, all volatile; every word an aligned 32-bit access in un-cached
 * DP RAM (csd_eq_ring.c rule), so torn reads are impossible by width and a
 * seq change is the publication event. The EQ task's adopt loop additionally
 * DOUBLE-READS the header and every payload word against the seq window: an
 * image is adopted only when a full read observed no word changing at all,
 * so even against the two documented publishers (diagnostic thread posts,
 * audio core restores on a real rate change) a half-published or torn image
 * is impossible -- the worst a publisher race can do is make the task
 * re-adopt one generation later, and every adopted image is complete.
 *
 * VOLATILE BY CONTRACT: RAM only, never journalled or persisted. A reboot,
 * any PRESet write, or a sample-rate change returns the engine to the
 * shipped default (unity). A board cannot sit in a shipping configuration
 * wearing a tester kernel.
 *
 * Applied state (k_src, k_live, the engine) is task-written; the SCPI
 * readback answers from the REQUEST side (what the host composed) exactly
 * like PRESet? reports the posted request, and the task-applied truth is
 * INFO? field eq_kernel_src. */
extern volatile int32_t  csd_k_word[CSD_KERNEL_MAX_TAPS]; /* host-composed kernel; defined in csd_dsp_store.c (.dp.bss.large) */
#define k_word csd_k_word
static volatile unsigned k_id    = CSD_KERNEL_ID_BIQUAD;
static volatile unsigned k_taps  = 5;
static volatile unsigned k_width = CSD_KERNEL_WIDTH_BITS;
static volatile unsigned k_q     = CSD_KERNEL_Q_SHIFT;
static volatile unsigned k_en;          /* 1 = override in force               */
static volatile unsigned k_rate;        /* rate idx the post was made for      */
static volatile unsigned k_seq;         /* bumped after every publication      */
static volatile unsigned k_src;         /* 0 shipped default, 1 override (task) */

/* Host kernels are Q1.30 with lsh 0 by contract (width/q validated against
 * the descriptor at the aperture; they are deliberately not host-settable
 * beyond the header fields, which must match these constants). */
#define CSD_BQ_HOST_LSH 0

/* Witness globals are defined unconditionally further down this TU (the SCPI
 * file reads them); forward-declare the two the config/clear paths bump. */
extern unsigned g_csd_bq_cfg_calls, g_csd_bq_clear_calls;

/* Target table -- THE single source of truth for what the aperture accepts.
 * The SCPI FORMat? reply and the container validator both read these
 * constants through the getters below, so layout, limits and enforcement
 * cannot drift apart. Unknown id, wrong taps, wrong width, wrong q, or a
 * length that does not match the header are all rejected at the aperture
 * BEFORE any state moves: the store never sees an invalid image. */
unsigned csd_dsp_kernel_max_taps_for(unsigned id)
{
    if (id == CSD_KERNEL_ID_BIQUAD) return 5u;
    if (id == CSD_KERNEL_ID_FIR)    return CSD_KERNEL_FIR_TAPS;
    return 0u;                              /* unknown kernel object */
}
unsigned csd_dsp_kernel_container_max_taps(void) { return CSD_KERNEL_MAX_TAPS; }

/* ---- measured per-rate FIR ceiling (2026-11-10) -----------------------
 * Indexed by the DSP rate index (csd_dsp_bq_rates[]: 0:44100 1:48000 2:88200
 * 3:96000 4:176400 5:192000 6:352800 7:384000). Every tier is MEASURED at the
 * faster member of its family (see the header block in csd_dsp.h), so the
 * 44.1 kHz column inherits the 48 kHz number and 352.8 kHz the 384 kHz one --
 * both have a LONGER frame period than the rate the limit was measured at, so
 * the inheritance errs safe. An unindexed rate gets the tightest tier.
 *
 * CSD_KERNEL_RATE_LIMITS_OFF exists only for the bench measurement build that
 * produced the table: with it the aperture admits any container up to the
 * physical ceiling, which is the only way to MEASURE a kernel the shipped
 * image would refuse. Never define it in a shipping build. */
static const unsigned fir_limit_idx[CSD_BQ_NUM_RATES] = {
    CSD_KERNEL_FIR_TAPS_AT_48,   /* 44100  (tier measured at 48000)  */
    CSD_KERNEL_FIR_TAPS_AT_48,   /* 48000                             */
    CSD_KERNEL_FIR_TAPS_AT_96,   /* 88200  (tier measured at 96000)  */
    CSD_KERNEL_FIR_TAPS_AT_96,   /* 96000                             */
    CSD_KERNEL_FIR_TAPS_AT_192,  /* 176400 (tier measured at 192000) */
    CSD_KERNEL_FIR_TAPS_AT_192,  /* 192000                            */
    CSD_KERNEL_FIR_TAPS_AT_384,  /* 352800 (tier measured at 384000) */
    CSD_KERNEL_FIR_TAPS_AT_384,  /* 384000                            */
};

unsigned csd_dsp_kernel_rate_limit(unsigned id)
{
    if (id == CSD_KERNEL_ID_BIQUAD) return 5u;      /* structural, not budgeted */
    if (id != CSD_KERNEL_ID_FIR)    return 0u;
#ifdef CSD_KERNEL_RATE_LIMITS_OFF
    return CSD_KERNEL_FIR_TAPS;
#else
    const unsigned i = csd_eq_cfg_idx_get();
    return (i < CSD_BQ_NUM_RATES) ? fir_limit_idx[i] : CSD_KERNEL_FIR_TAPS_AT_384;
#endif
}

/* The stream rate the current limit was derived for, for FORMat?. */
unsigned csd_dsp_kernel_rate_limit_rate_hz(void)
{
    const unsigned i = csd_eq_cfg_idx_get();
    return (i < CSD_BQ_NUM_RATES) ? csd_dsp_bq_rates[i].rate : 0u;
}

/* Is (id, taps) adoptable? Both the aperture (before publishing) and the EQ
 * task (before adopting) run this, so a container that slipped past one check
 * cannot be applied by the other -- the failure mode this replaces was a SHORT
 * container accepted by the store and then silently refused by csd_dsp_bq_config,
 * i.e. a successful write that adopted nothing. Taps below the ceiling are
 * legal (a 4-tap FIR is a real filter); 0 taps, over-capacity, and the biquad's
 * fixed 5 are the refusals. */
static unsigned kernel_taps_legal(unsigned id, unsigned tp)
{
    const unsigned cap = csd_dsp_kernel_max_taps_for(id);
    if (cap == 0u || tp == 0u || tp > cap) return 0u;
    if (id == CSD_KERNEL_ID_BIQUAD) return tp == 5u;
    return tp <= csd_dsp_kernel_rate_limit(id);
}
unsigned csd_dsp_kernel_taps_legal(unsigned id, unsigned tp);   /* csd_dsp.h */
unsigned csd_dsp_kernel_taps_legal(unsigned id, unsigned tp) { return kernel_taps_legal(id, tp); }

void csd_dsp_bq_config(int rate_idx)
{
    bq_ready = 0;
#ifdef CSD_EQ_DIAG_WITNESS
    g_csd_bq_cfg_calls++;
#endif
    if (rate_idx < 0 || (unsigned)rate_idx >= CSD_BQ_NUM_RATES) return;
    const unsigned ri = (unsigned)rate_idx;

    /* Adopt a host-composed kernel only if one is in force FOR THIS RATE.
     * Acceptance requires a quiescent observation: seq unchanged across the
     * whole read AND every header field and payload word identical between
     * its two reads (see the aperture block above). A publisher landing
     * mid-copy sends us around the poll again instead of half-applying. */
    unsigned s = 0u, en = 0u, rt = 0u, id = 0u, tp = 0u, wd = 0u, qq = 0u;
    unsigned good = 0u;
    /* k_live is the staging buffer: task-owned, so copying straight into it
     * adds no stack (a 16 KB frame array here would blow the xcc stack bound)
     * and a failed attempt is harmless -- every rejection path below
     * overwrites the kernel fields from the shipped default anyway. */
    for (unsigned tries = 0; tries < 4u && !good; tries++) {
        s = k_seq;
        en = k_en; rt = k_rate; id = k_id; tp = k_taps; wd = k_width; qq = k_q;
        if (tp > CSD_KERNEL_MAX_TAPS) continue;      /* impossible; belt */
        for (unsigned i = 0; i < tp; i++) k_live[i] = k_word[i];
        if (k_seq != s || k_en != en || k_rate != rt || k_id != id ||
            k_taps != tp || k_width != wd || k_q != qq) continue;
        good = 1u;
        for (unsigned i = 0; i < tp; i++)
            if (k_word[i] != k_live[i]) { good = 0u; break; }
    }
    if (good && en && rt == ri && wd == CSD_KERNEL_WIDTH_BITS &&
        qq == CSD_KERNEL_Q_SHIFT &&
        kernel_taps_legal(id, tp) && tp != 0u) {
        k_engine = id;
        k_live_taps = tp;
        if (id == CSD_KERNEL_ID_BIQUAD) {
            bq_c = k_live;
            bq_lsh = CSD_BQ_HOST_LSH;
        }
        k_src = 1u;
    } else {
        bq_c = csd_dsp_bq_rates[ri].coeffs;   /* shipped default (unity) */
        bq_lsh = csd_dsp_bq_rates[ri].lsh;
        k_engine = CSD_KERNEL_ID_BIQUAD;
        k_live_taps = 5u;
        k_src = 0u;
    }
    memset(bq_state, 0, sizeof bq_state);
    memset(fir_hist, 0, sizeof fir_hist);
    fir_head = 0u;
    bq_ready = 1;
#ifdef CSD_DSP_DEBUG
    printf("CSDHQ: config rate=%u engine=%u taps=%u lsh=%d c=[%d,%d,%d,%d,%d]\n",
           csd_dsp_bq_rates[ri].rate, k_engine, k_live_taps, (int)bq_lsh,
           (int)bq_c[0], (int)bq_c[1], (int)bq_c[2], (int)bq_c[3], (int)bq_c[4]);
#endif
}

void csd_dsp_bq_clear(void) { bq_ready = 0; bq_c = 0; k_src = 0u;
#ifdef CSD_EQ_DIAG_WITNESS
    g_csd_bq_clear_calls++;
#endif
}
int  csd_dsp_bq_ready(void) { return bq_ready; }

/* ---- aperture entry points (see the aperture block above) --------------
 * All run on the DIAGNOSTIC thread except csd_dsp_bq_coef_restore (also the
 * AUDIO thread, on a real rate change) and csd_dsp_bq_coef_seq (EQ task
 * poll). The readback helpers are called from the SCPI thread. Pointer
 * parameters are fine here: every caller is C (csd_diag_scpi.c); the
 * scalar-only rule governs functions called from XC (csd_trim_store.h
 * lesson). */
static const int32_t *coef_default_for(unsigned rate_idx)
{
    if (rate_idx >= CSD_BQ_NUM_RATES) rate_idx = 0;
    return csd_dsp_bq_rates[rate_idx].coeffs;
}

/* Publish one complete kernel container (validated by the caller at the
 * aperture: id known, taps == target capacity, width/q match the
 * descriptor). One call = one publication = one adoption by the EQ task. */
void csd_dsp_kernel_publish(unsigned id, unsigned taps, const int32_t *coef)
{
    const unsigned rt = csd_eq_cfg_idx_get();
    for (unsigned i = 0; i < taps; i++) k_word[i] = coef[i];
    k_id = id; k_taps = taps;
    k_width = CSD_KERNEL_WIDTH_BITS; k_q = CSD_KERNEL_Q_SHIFT;
    k_rate = rt;
    k_en = 1u;
    k_seq++;                                   /* payload first, seq last */
}

void csd_dsp_bq_coef_restore(void)
{
    if (!k_en) return;                         /* nothing to clear: no churn */
    k_en = 0u;                                 /* back to the SHIPPED DEFAULT:
                                                * unity, not any old kernel   */
    k_seq++;
}

int32_t csd_dsp_bq_coef_read(unsigned idx)
{
    /* Debug/query-only readback of the REQUEST side, per word (the whole
     * kernel travels base64; the indexed form is kept solely so a human can
     * inspect one word at a time). Biquad index space is 0..4; the FIR
     * target answers any idx < taps of a loaded FIR, else 0. */
    if (k_en) {
        if (idx < k_taps) return k_word[idx];
        return 0;
    }
    if (idx >= 5u) return 0;
    return coef_default_for(csd_eq_cfg_idx_get())[idx];
}

unsigned csd_dsp_bq_coef_seq(void)       { return k_seq; }
unsigned csd_dsp_bq_diag_kernel_src(void) { return k_src; }
unsigned csd_dsp_kernel_request_id(void)   { return k_en ? k_id : CSD_KERNEL_ID_BIQUAD; }
unsigned csd_dsp_kernel_request_taps(void) { return k_en ? k_taps : 5u; }
unsigned csd_dsp_kernel_request_en(void)   { return k_en; }

/* Witnesses for the 384 kHz left-channel EQ fault (2026-11-10). Globals are
 * defined UNCONDITIONALLY (csd_diag_scpi.c does not receive CSD_EXTRA_FLAGS,
 * same rule as g_csd_xfer_* in xua_audiohub.xc); only the increments are
 * behind CSD_EQ_DIAG_WITNESS. Writers: audiohub core. Reader: diag thread,
 * informational only. REVERT: delete these and the guarded increments, and
 * drop the tail fields they feed from DIAG:UPSAMPLing:INFO?. */
unsigned g_csd_bq_calls[2];      /* engine invocations per channel */
unsigned g_csd_bq_in_or[2];      /* OR of every input word, per channel */
unsigned g_csd_bq_in_last[2];    /* last input word seen, per channel */
unsigned g_csd_bq_out_last[2];   /* last output word returned, per channel */
unsigned g_csd_bq_out_or[2];     /* OR of every output word, per channel */
unsigned g_csd_bq_in_nz[2];      /* nonzero-input census, per channel */
unsigned g_csd_bq_cfg_calls;     /* bq_config invocations */
unsigned g_csd_bq_clear_calls;   /* bq_clear invocations */

/* Filter one stereo frame in place, dispatching on the adopted engine.
 *
 * ENGINE COST, measured 2026-11-10 on this board (CSD_EQ_DIAG_WITNESS,
 * DIAG:UPSAMPLing:INFO? fields eq_ticks_max / xt_ubm): the two per-frame
 * adsp_biquad VPU asm calls cost ~98 reference (10 ns) ticks. At 384 kHz the
 * audiohub loop must complete a frame in ~2083 core cycles (2.6 us) and the
 * preset-0 control already sits at the wire rate with no measurable margin,
 * so NOTHING of this file runs there: the frame hook pays one csd_eq_handoff
 * call (csd_eq_ring.c) and the whole engine below runs on the EQ task core.
 * The scalar C biquad is the same DF-I arithmetic the VPU asm implements
 * (acc = c0*x + c1*s0 + c2*s1 + c3*y1 + c4*y2, y = acc >> (30 - lsh)) at
 * ~1/5 the per-frame cost. VPU asm is kept behind CSD_DSP_BQ_VPU for offline
 * comparison only. */
void csd_dsp_bq_frame(unsigned s[2])
{
    if (!bq_ready) return;
    if (k_engine == CSD_KERNEL_ID_FIR) {
        const int32_t *c = k_live;
        const unsigned n = k_live_taps;
        unsigned h = fir_head;
        for (unsigned ch = 0; ch < 2; ch++) {
#ifdef CSD_EQ_DIAG_WITNESS
            g_csd_bq_calls[ch]++;
            g_csd_bq_in_or[ch]  |= s[ch];
            g_csd_bq_in_last[ch] = s[ch];
            if (s[ch]) g_csd_bq_in_nz[ch]++;
#endif
            /* Descending reads from the head, split into two branch-free
             * linear passes at the array bottom. Equivalent tap-for-tap to
             * the original `acc += c[i]*hist[(h-i) mod MAX]` loop (tap i
             * pairs with hist[(h-i) mod MAX] in both), and single-writer
             * task-owned state makes the split safe. Why not the modular
             * loop: xobjdump of the 2026-11-10 capacity build shows the
             * compiler emitting a shl+add address rebase plus NOP padding
             * per tap (~40 core cycles/MAC; eq_ticks_max 10,362 at 1024
             * taps); the per-tap-wrap sliding-pointer variant measured
             * worse still (12,914) because the wrap compare cannot
             * pipeline. Same MACs, same int64 acc, same >>30, same slot. */
            int32_t *hist = fir_hist[ch];
            int64_t acc = 0;
            {
                unsigned i = 0u;
                const int32_t *cp = &c[0];
                const int32_t *hp = &hist[h];
                const unsigned m = (n <= h) ? n : (h + 1u);
                while (i < m) {
                    acc += (int64_t)(*cp) * (int64_t)(*hp);
                    cp++; hp--; i++;
                }
                hp = &hist[CSD_KERNEL_MAX_TAPS - 1u];
                while (i < n) {
                    acc += (int64_t)(*cp) * (int64_t)(*hp);
                    cp++; hp--; i++;
                }
            }
            hist[h] = (int32_t)s[ch];
            const int32_t y = (int32_t)(acc >> 30);
            s[ch] = (unsigned)y;
#ifdef CSD_EQ_DIAG_WITNESS
            g_csd_bq_out_last[ch] = s[ch];
            g_csd_bq_out_or[ch] |= s[ch];
#endif
        }
        fir_head = (h + 1u == CSD_KERNEL_MAX_TAPS) ? 0u : h + 1u;
        return;
    }
    const int sh = 30 - (int)bq_lsh;               /* Q(1+lsh).30 contract */
    for (unsigned ch = 0; ch < 2; ch++) {
#ifdef CSD_EQ_DIAG_WITNESS
        g_csd_bq_calls[ch]++;
        g_csd_bq_in_or[ch]  |= s[ch];
        g_csd_bq_in_last[ch] = s[ch];
        if (s[ch]) g_csd_bq_in_nz[ch]++;
#endif
#ifdef CSD_DSP_BQ_VPU
        s[ch] = (unsigned)adsp_biquad((int32_t)s[ch], (q2_30 *)bq_c,
                                      bq_state[ch], (left_shift_t)bq_lsh);
#else
        int32_t *stt = bq_state[ch];
        int64_t acc = (int64_t)bq_c[0] * (int32_t)s[ch]
                    + (int64_t)bq_c[1] * stt[0]
                    + (int64_t)bq_c[2] * stt[1]
                    + (int64_t)bq_c[3] * stt[2]
                    + (int64_t)bq_c[4] * stt[3];
        int32_t y = (int32_t)(acc >> sh);
        stt[1] = stt[0]; stt[0] = (int32_t)s[ch];
        stt[3] = stt[2]; stt[2] = y;
        s[ch] = (unsigned)y;
#endif
#ifdef CSD_EQ_DIAG_WITNESS
        g_csd_bq_out_last[ch] = s[ch];
        g_csd_bq_out_or[ch] |= s[ch];
#endif
    }
}

#ifdef CSD_DSP_BQ_SELFTEST
/* Host/target-portable check of the integer arithmetic the asm implements:
 * direct form I in Q(1+lsh).30. Returns a packed fingerprint so it can be
 * reported over the mic path when no console is available:
 *   bits 31..20: |y| peak at 1 kHz full-scale, >>19  (sane filter: ~4000)
 *   bits 19..10: 1 if the DC (bass) path kept a full-scale step intact
 *   bits  9.. 0: nonzero-output census over 512 samples, >>1
 * A dead filter reads 0 in every field; a saturating one overflows the peak. */
unsigned csd_dsp_bq_selftest(int rate_idx)
{
    if (rate_idx < 0 || (unsigned)rate_idx >= CSD_BQ_NUM_RATES) return 0xDEADu;
    const int32_t *c = csd_dsp_bq_rates[rate_idx].coeffs;
    const int sh = 30 - csd_dsp_bq_rates[rate_idx].lsh;   /* Q(1+lsh).30 */
    int32_t st[8];
    unsigned peak = 0, dc_ok = 0, nz = 0;
    memset(st, 0, sizeof st);
    /* 1 kHz at full scale: must stay inside int32 (treble is the -6 dB band). */
    for (unsigned i = 0; i < 4096u; i++) {
        /* integer sine approximation is unnecessary: an LCG-driven full-scale
         * sequence exercises the same coefficient paths and cannot overflow
         * silently. Use ±full-scale alternating (Nyquist), the worst case for
         * a filter whose HF gain is 0.5. */
        int32_t x = (i & 1u) ? -2147483647 : 2147483647;
        int64_t acc = (int64_t)c[0] * x + (int64_t)c[1] * st[0]
                    + (int64_t)c[2] * st[1] + (int64_t)c[3] * st[2]
                    + (int64_t)c[4] * st[3];
        int32_t y = (int32_t)(acc >> sh);
        st[1] = st[0]; st[0] = x;
        st[3] = st[2]; st[2] = y;
        unsigned ay = (y < 0) ? (unsigned)(-y) : (unsigned)y;
        if (ay > peak) peak = ay;
    }
    /* DC: a full-scale step must pass at ~0 dB (bass is the unity band). */
    memset(st, 0, sizeof st);
    for (unsigned i = 0; i < 8192u; i++) {
        int64_t acc = (int64_t)c[0] * 2147483647 + (int64_t)c[1] * st[0]
                    + (int64_t)c[2] * st[1] + (int64_t)c[3] * st[2]
                    + (int64_t)c[4] * st[3];
        int32_t y = (int32_t)(acc >> sh);
        st[1] = st[0]; st[0] = 2147483647;
        st[3] = st[2]; st[2] = y;
        if (i > 4096u && (unsigned)y > 1073741824u) dc_ok = 1;
        if (y != 0) nz++;
    }
    return ((peak >> 19) << 20) | (dc_ok << 10) | (nz >> 1);
}
#endif
