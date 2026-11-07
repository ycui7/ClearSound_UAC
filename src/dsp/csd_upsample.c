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
    uint32_t i, k, idx;
    for (i = 0; i < n; i++) {
        s->hist[s->pos] = in[i];
        int64_t acc = 0;
        idx = s->pos;
        for (k = 0; k < CSD_BL_TAPS; k++) {
            acc += (int64_t)csd_bl_coef[k] * (int64_t)s->hist[idx];
            idx = (idx == 0u) ? CSD_BL_TAPS - 1u : idx - 1u;
        }
        acc >>= 30;
        if (acc > 2147483647LL) acc = 2147483647LL;
        if (acc < -2147483648LL) acc = -2147483648LL;
        out[i] = (int32_t)acc;
        s->pos = (s->pos + 1u) % CSD_BL_TAPS;
    }
}
