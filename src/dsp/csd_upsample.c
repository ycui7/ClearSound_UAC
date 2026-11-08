#include "csd_upsample.h"

uint32_t csd_up_out_fs(uint32_t fs_in) {
    switch (fs_in) {
    case 48000u:
    case 96000u:
    case 192000u: return CSD_DSP_FS_OUT;      /* 48 series  -> 384 kHz   */
    case 44100u:
    case 88200u:
    case 176400u: return CSD_DSP_FS_OUT_441;  /* 44.1 series -> 352.8 kHz */
    default:      return 0u;                  /* not a family member      */
    }
}

/* The prototype is defined per ratio and normalised against its own input
 * rate, so 44.1 kHz and 48 kHz share the L=8 table exactly as they share the
 * halfband. Selecting the table by rate instead would silently deny the whole
 * 44.1 kHz family a prototype. */
static const int32_t *coef_for(uint32_t fs_in) {
    switch (csd_up_out_fs(fs_in) / (fs_in ? fs_in : 1u)) {
    case 8u: return csd_up48_coef;
    case 4u: return csd_up96_coef;
    case 2u: return csd_up192_coef;
    default: return 0;
    }
}

static uint32_t tpp_for(uint32_t fs_in) {
    switch (csd_up_out_fs(fs_in) / (fs_in ? fs_in : 1u)) {
    case 8u: return CSD_UP48_TAPS_PER_PHASE;
    case 4u: return CSD_UP96_TAPS_PER_PHASE;
    case 2u: return CSD_UP192_TAPS_PER_PHASE;
    default: return 0u;
    }
}

uint32_t csd_up_ratio(uint32_t fs_in) {
    const uint32_t out = csd_up_out_fs(fs_in);
    if (out == 0u || fs_in == 0u) return 0u;
    const uint32_t r = out / fs_in;
    /* Exact integer ratios only. A rate that divides unevenly is not in a
     * family, and interpolating it with an 8-tap-phase kernel would be a wrong
     * answer rather than a degraded one. */
    return (r == 8u || r == 4u || r == 2u) ? r : 0u;
}

void csd_up_reset(csd_up_state *s, uint32_t fs_in) {
    uint32_t i;
    for (i = 0; i < CSD_UP_MAX_TAPS_PER_PHASE; i++) s->hist[i] = 0;
    s->n_hist = tpp_for(fs_in);
}

void csd_up_process(csd_up_state *s, uint32_t fs_in,
                    const int32_t *inbuf, uint32_t n_in, int32_t *outbuf) {
    const uint32_t L = csd_up_ratio(fs_in);
    const int32_t *c = coef_for(fs_in);
    const uint32_t tpp = s->n_hist;
    uint32_t n, j, k;

    if (!c || !L) return;              /* unsupported rate: emit nothing */

    for (n = 0; n < n_in; n++) {
        /* push the new input sample, shifting the history down */
        for (k = tpp - 1u; k > 0u; k--) s->hist[k] = s->hist[k - 1u];
        s->hist[0] = inbuf[n];

        for (j = 0; j < L; j++) {
            int64_t acc = 0;
            for (k = 0; k < tpp; k++) {
                acc += (int64_t)c[k * L + j] * (int64_t)s->hist[k];
            }
            acc >>= 30;                /* Q30 coefficients */
            if (acc > 2147483647LL) acc = 2147483647LL;
            if (acc < -2147483648LL) acc = -2147483648LL;
            outbuf[n * L + j] = (int32_t)acc;
        }
    }
}

void csd_bl_reset(csd_bl_state *s) {
    uint32_t i;
    for (i = 0; i < CSD_BL_TAPS; i++) s->hist[i] = 0;
    s->pos = 0;
}

void csd_bl_process(csd_bl_state *s, const int32_t *inbuf, uint32_t n, int32_t *outbuf) {
    /* Symmetric kernel, folded: c[k] == c[TAPS-1-k], so each multiply serves the
     * tap from the front and the tap from the back. 511 taps -> 255 multiplies
     * plus the centre tap, i.e. half the work for identical output. */
    const uint32_t half = CSD_BL_TAPS / 2u;
    uint32_t i, k, i1, i2;
    for (i = 0; i < n; i++) {
        s->hist[s->pos] = inbuf[i];
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
        outbuf[i] = (int32_t)acc;
        s->pos = (s->pos + 1u) % CSD_BL_TAPS;
    }
}

/* ---------------- 2x halfband stage ---------------------------------- */
void csd_hb_reset(csd_hb_state *s) {
    for (uint32_t i = 0; i < CSD_HB_HIST; i++) s->hist[i] = 0;
}

void csd_hb2_process(csd_hb_state *s, const int32_t *inbuf, uint32_t n_in, int32_t *outbuf) {
    /* 161 taps (N = 4m+1) with the centre at index 80, an EVEN index. So the
     * centre aligns with EVEN output samples: those are the identity branch.
     * The interpolated phase is the ODD outputs, built from the 80 odd-indexed
     * taps as 40 symmetric pairs; the 80 even-indexed taps are structural zeros.
     *   y[2n]   = x[n-40]
     *   y[2n+1] = sum q=0..39  h[2q+1] * (x[n-q] + x[n-79+q])
     */
    for (uint32_t n = 0; n < n_in; n++) {
        for (uint32_t k = CSD_HB_HIST - 1u; k > 0u; k--) s->hist[k] = s->hist[k - 1u];
        s->hist[0] = inbuf[n];

        int64_t acc = 0;
        for (uint32_t q = 0; q < 40u; q++) {
            acc += (int64_t)csd_hb_coef[2u * q + 1u]
                 * ((int64_t)s->hist[q] + (int64_t)s->hist[79u - q]);
        }
        acc >>= 30;
        if (acc > 2147483647LL) acc = 2147483647LL;
        if (acc < -2147483648LL) acc = -2147483648LL;

        outbuf[2u * n] = s->hist[40];           /* identity, delay 40 = centre/2 */
        outbuf[2u * n + 1u] = (int32_t)acc;     /* interpolated */
    }
}

/* ---------------- 2x cascade: 8x = 3 stages, 4x = 2, 2x = 1 -------------- */
void csd_cascade_reset(csd_cascade_state *c) {
    for (uint32_t i = 0; i < 3u; i++) csd_hb_reset(&c->st[i]);
}

/* Stage chain, chosen so no stage writes over samples it still has to read:
 *   192 kHz   in -> out
 *   96 kHz    in -> tmp -> out
 *   48 kHz    in -> tmp -> tmp2 -> out
 */
void csd_cascade_process(csd_cascade_state *c, uint32_t fs_in,
                         const int32_t *inbuf, uint32_t n_in,
                         int32_t *outbuf, int32_t *tmp, int32_t *tmp2) {
    /* Stage count comes from the ratio, so both families use this path
     * unchanged: 176.4 -> 352.8 kHz takes one stage exactly as 192 -> 384 does.
     * Chosen so no stage writes over samples it still has to read. */
    switch (csd_up_ratio(fs_in)) {
    case 2u:
        csd_hb2_process(&c->st[0], inbuf, n_in, outbuf);
        break;
    case 4u:
        csd_hb2_process(&c->st[0], inbuf, n_in, tmp);
        csd_hb2_process(&c->st[1], tmp, 2u * n_in, outbuf);
        break;
    case 8u:
        csd_hb2_process(&c->st[0], inbuf, n_in, tmp);
        csd_hb2_process(&c->st[1], tmp, 2u * n_in, tmp2);
        csd_hb2_process(&c->st[2], tmp2, 4u * n_in, outbuf);
        break;
    default:
        /* Not a family rate: leave the output buffer alone rather than emit
         * something at the wrong rate. */
        break;
    }
}
