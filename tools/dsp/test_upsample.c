/* Host test for the CSD upsampling core: proves the behaviour a listener would
 * notice, using only the shipped fixed-point tables. gcc -O2, no XMOS needed. */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "csd_upsample.h"

#define N_IN 4096

static double goertzel_db(const int32_t *x, int n, double f, double fs) {
    double w = 2.0 * M_PI * f / fs, cw = cos(w), coeff = 2.0 * cw;
    double s0 = 0, s1 = 0, s2 = 0;
    for (int i = 0; i < n; i++) { s0 = x[i] / 2147483648.0 + coeff * s1 - s2; s2 = s1; s1 = s0; }
    double mag = sqrt(s1 * s1 + s2 * s2 - coeff * s1 * s2);
    return 20.0 * log10(mag / (n / 2.0) + 1e-18);
}

static int32_t in[N_IN];
static int32_t mid[N_IN * 8];
static int32_t out[N_IN * 8];
static csd_up_state up;
static csd_bl_state bl;

/* Reference: the UNFOLDED convolution. The folded path must match this bit for
 * bit -- folding changes how much work is done, never the result. */
static int32_t bl_naive(const int32_t *hist, uint32_t pos) {
    int64_t acc = 0;
    uint32_t idx = pos;
    for (uint32_t k = 0; k < CSD_BL_TAPS; k++) {
        acc += (int64_t)csd_bl_coef[k] * (int64_t)hist[idx];
        idx = (idx == 0u) ? CSD_BL_TAPS - 1u : idx - 1u;
    }
    acc >>= 30;
    if (acc > 2147483647LL) acc = 2147483647LL;
    if (acc < -2147483648LL) acc = -2147483648LL;
    return (int32_t)acc;
}

int main(void) {
    int fail = 0;
    const double FS_IN = 48000.0, FS_OUT = 384000.0;
    const int32_t FULL = 2147483647;

    /* 1) DC passthrough: a constant must come out constant, not droop */
    for (int i = 0; i < N_IN; i++) in[i] = FULL / 4;
    csd_up_reset(&up, 48000);
    csd_up_process(&up, 48000, in, N_IN, mid);
    double dc = mid[(N_IN * 8) - 1] / (double)(FULL / 4);
    printf("DC gain            %.4f   (want 1.0000 +/-0.01)  %s\n", dc,
           fabs(dc - 1.0) < 0.01 ? "PASS" : "FAIL");
    if (fabs(dc - 1.0) >= 0.01) fail++;

    /* 2) group delay: an impulse must emerge at (N-1)/2 output samples */
    memset(in, 0, sizeof in);
    in[8] = FULL / 2;
    csd_up_reset(&up, 48000);
    csd_up_process(&up, 48000, in, 256, mid);
    int peak = 0; long best = -1;
    for (int i = 0; i < 256 * 8; i++) if (labs((long)mid[i]) > best) { best = labs((long)mid[i]); peak = i; }
    int expect = 8 * 8 + (int)((CSD_UP48_PHASES * CSD_UP48_TAPS_PER_PHASE - 1) / 2);
    printf("impulse peak       %d     (expect %d +/-2)                 %s\n", peak, expect,
           abs(peak - expect) <= 2 ? "PASS" : "FAIL");
    if (abs(peak - expect) > 2) fail++;

    /* 3) a 1 kHz tone survives, and its 47 kHz image does not */
    for (int i = 0; i < N_IN; i++) in[i] = (int32_t)(FULL / 4 * sin(2.0 * M_PI * 1000.0 * i / FS_IN));
    csd_up_reset(&up, 48000);
    csd_up_process(&up, 48000, in, N_IN, mid);
    double a1k = goertzel_db(mid + 8192, 24576, 1000.0, FS_OUT);
    double img = goertzel_db(mid + 8192, 24576, 47000.0, FS_OUT);
    printf("1 kHz level        %.2f dBFS                          %s\n", a1k,
           (a1k > -13.0 && a1k < -11.0) ? "PASS" : "FAIL");
    if (!(a1k > -13.0 && a1k < -11.0)) fail++;
    printf("image at 47 kHz    %.1f dB   (want < -80)               %s\n", img,
           img < -80.0 ? "PASS" : "FAIL");
    if (img >= -80.0) fail++;

    /* 4) band limit: on its own DC stimulus, unity must survive */
    for (int i = 0; i < N_IN * 8; i++) mid[i] = FULL / 4;   /* mid is elsewhere the tone */
    csd_bl_reset(&bl);
    csd_bl_process(&bl, mid, N_IN * 8, out);
    double bl_dc = out[(N_IN * 8) - 1] / (double)mid[(N_IN * 8) - 1];
    printf("BL DC gain         %.3f                               %s\n", bl_dc,
           fabs(bl_dc - 1.0) < 0.02 ? "PASS" : "FAIL");
    if (fabs(bl_dc - 1.0) >= 0.02) fail++;

    /* 5) the folded kernel must be bit-identical to the unfolded convolution */
    {
        static int32_t sig[4096], a[4096], b[4096];
        csd_bl_state bl2;
        csd_bl_reset(&bl); csd_bl_reset(&bl2);
        for (int i = 0; i < 4096; i++) sig[i] = (int32_t)((i * 2654435761u) ^ 0x5bf03635u);
        csd_bl_process(&bl, sig, 4096, a);
        for (int i = 0; i < 4096; i++) {
            bl2.hist[bl2.pos] = sig[i];
            b[i] = bl_naive(bl2.hist, bl2.pos);
            bl2.pos = (bl2.pos + 1u) % CSD_BL_TAPS;
        }
        int mism = 0;
        for (int i = 0; i < 4096; i++) if (a[i] != b[i]) mism++;
        printf("folded == unfolded  %d mismatches of 4096  %s\n", mism, mism == 0 ? "PASS" : "FAIL");
        if (mism) fail++;
    }

    printf("\n%s\n", fail ? "RESULT: FAIL" : "RESULT: PASS");
    return fail ? 1 : 0;
}
