#include "csd_upsample.h"

/* One table of the three; the ratio picks it. */
static const int32_t *coef_for(uint32_t fs_in) {
    switch (fs_in) {
    case 48000u: return csd_up48_coef;
    case 96000u: return csd_up96_coef;
    case 192000u: return csd_up192_coef;
    default:     return 0;
    }
}

static uint32_t tpp_for(uint32_t fs_in) {
    switch (fs_in) {
    case 48000u: return CSD_UP48_TAPS_PER_PHASE;
    case 96000u: return CSD_UP96_TAPS_PER_PHASE;
    case 192000u: return CSD_UP192_TAPS_PER_PHASE;
    default:     return 0u;
    }
}

uint32_t csd_up_ratio(uint32_t fs_in) {
    return CSD_DSP_FS_OUT / (fs_in ? fs_in : 1u) == 8u ? 8u
         : fs_in == 96000u ? 4u
         : fs_in == 192000u ? 2u
         : fs_in == 48000u ? 8u : 0u;
}

void csd_up_reset(csd_up_state *s, uint32_t fs_in) {
    uint32_t i;
    for (i = 0; i < CSD_UP_MAX_TAPS_PER_PHASE; i++) s->hist[i] = 0;
    s->n_hist = tpp_for(fs_in);
}

void csd_up_process(csd_up_state *s, uint32_t fs_in,
                    const int32_t *in, uint32_t n_in, int32_t *out) {
    const uint32_t L = csd_up_ratio(fs_in);
    const int32_t *c = coef_for(fs_in);
    const uint32_t tpp = s->n_hist;
    uint32_t n, j, k;

    if (!c || !L) return;              /* unsupported rate: emit nothing */

    for (n = 0; n < n_in; n++) {
        /* push the new input sample, shifting the history down */
        for (k = tpp - 1u; k > 0u; k--) s->hist[k] = s->hist[k - 1u];
        s->hist[0] = in[n];

        for (j = 0; j < L; j++) {
            int64_t acc = 0;
            for (k = 0; k < tpp; k++) {
                acc += (int64_t)c[k * L + j] * (int64_t)s->hist[k];
            }
            acc >>= 30;                /* Q30 coefficients */
            if (acc > 2147483647LL) acc = 2147483647LL;
            if (acc < -2147483648LL) acc = -2147483648LL;
            out[n * L + j] = (int32_t)acc;
        }
    }
}

void csd_bl_reset(csd_bl_state *s) {
    uint32_t i;
    for (i = 0; i < CSD_BL_TAPS; i++) s->hist[i] = 0;
    s->pos = 0;
}

void csd_bl_process(csd_bl_state *s, const int32_t *in, uint32_t n, int32_t *out) {
    /* Symmetric kernel, folded: c[k] == c[TAPS-1-k], so each multiply serves the
     * tap from the front and the tap from the back. 511 taps -> 255 multiplies
     * plus the centre tap, i.e. half the work for identical output. */
    const uint32_t half = CSD_BL_TAPS / 2u;
    uint32_t i, k, i1, i2;
    for (i = 0; i < n; i++) {
        s->hist[s->pos] = in[i];
        int64_t acc = 0;
        i1 = s->pos;                          /* tap k,     walking back  */
        i2 = (s->pos + 1u) % CSD_BL_TAPS;     /* tap TAPS-1-k, walking fwd */
        for (k = 0; k < half; k++) {
            acc += (int64_t)csd_bl_coef[k] * ((int64_t)s->hist[i1] + (int64_t)s->hist[i2]);
            i1 = (i1 == 0u) ? CSD_BL_TAPS - 1u : i1 - 1u;
            i2 = (i2 + 1u) % CSD_BL_TAPS;
        }
        acc += (int64_t)csd_bl_coef[half] * (int64_t)s->hist[(s->pos + CSD_BL_TAPS - half) % CSD_BL_TAPS];
        acc >>= 30;
        if (acc > 2147483647LL) acc = 2147483647LL;
        if (acc < -2147483648LL) acc = -2147483648LL;
        out[i] = (int32_t)acc;
        s->pos = (s->pos + 1u) % CSD_BL_TAPS;
    }
}

/* ---------------- 2x halfband stage ---------------------------------- */
void csd_hb_reset(csd_hb_state *s) {
    for (uint32_t i = 0; i < CSD_HB_HIST; i++) s->hist[i] = 0;
}

void csd_hb2_process(csd_hb_state *s, const int32_t *in, uint32_t n_in, int32_t *out) {
    /* Centre tap sits at index 63 (odd), so the centre aligns with ODD output
     * samples: those are the identity branch. The interpolated phase is the EVEN
     * outputs, built from the 64 even-indexed taps -- the odd-indexed ones are
     * the structural zeros and are never touched.
     *
     *   y[2n+1] = x[n]
     *   y[2n]   = sum_{k=0..63} h[2k] * x[n-k]      (32 symmetric pairs)
     */
    for (uint32_t n = 0; n < n_in; n++) {
        for (uint32_t k = CSD_HB_HIST - 1u; k > 0u; k--) s->hist[k] = s->hist[k - 1u];
        s->hist[0] = in[n];

        int64_t acc = 0;
        for (uint32_t k = 0; k < 32u; k++) {   /* h[2k] == h[2*(63-k)] */
            acc += (int64_t)csd_hb_coef[2u * k]
                 * ((int64_t)s->hist[k] + (int64_t)s->hist[63u - k]);
        }
        acc >>= 30;
        if (acc > 2147483647LL) acc = 2147483647LL;
        if (acc < -2147483648LL) acc = -2147483648LL;

        out[2u * n] = (int32_t)acc;          /* interpolated */
        out[2u * n + 1u] = s->hist[31];      /* identity, delayed to meet the interp phase */
    }
}

/* ---------------- 2x cascade to 384 kHz ------------------------------ */
void csd_cascade_reset(csd_cascade_state *c) {
    for (uint32_t i = 0; i < 3u; i++) csd_hb_reset(&c->st[i]);
}

/* Stage chain, chosen so no stage writes over samples it still has to read:
 *   192 kHz   in -> out
 *   96 kHz    in -> tmp -> out
 *   48 kHz    in -> tmp -> tmp2 -> out
 */
void csd_cascade_process(csd_cascade_state *c, uint32_t fs_in,
                         const int32_t *in, uint32_t n_in,
                         int32_t *out, int32_t *tmp, int32_t *tmp2) {
    if (fs_in == 192000u) {
        csd_hb2_process(&c->st[0], in, n_in, out);
    } else if (fs_in == 96000u) {
        csd_hb2_process(&c->st[0], in, n_in, tmp);
        csd_hb2_process(&c->st[1], tmp, 2u * n_in, out);
    } else if (fs_in == 48000u) {
        csd_hb2_process(&c->st[0], in, n_in, tmp);
        csd_hb2_process(&c->st[1], tmp, 2u * n_in, tmp2);
        csd_hb2_process(&c->st[2], tmp2, 4u * n_in, out);
    }
    /* any other rate emits nothing rather than a wrong-rate signal */
}
