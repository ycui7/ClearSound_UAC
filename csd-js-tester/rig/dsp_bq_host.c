/* dsp_bq_host.c -- host-side twin of the ClearSoundDac bass shelf, for the
 * bit-perfectness rig.
 *
 * WHY C AND NOT PYTHON: the filter is recursive, so it cannot be vectorised,
 * and a Python int list over 23M frames costs gigabytes -- the first version
 * of rig/dsp_bit_perfect.py was cgroup-OOM-killed mid-run (SIGTERM, rc=143)
 * exactly the failure mode documented for this box. The device runs this in
 * C, so the rig compares against C too: one fewer translation between the
 * claim and the measurement.
 *
 * THE ARITHMETIC IS THE ASM, not a float model. biquad.S under vsetc 12:
 *   each product   (x*c + 2**29) >> 30     <- VPU rounding, PER PRODUCT
 *   accumulate     int40 sum
 *   store          saturating extract into the y history (vstrpv)
 *   shl sample,lsh our coefficient table has lsh=0 at every rate (asserted)
 * This is NOT the process_int form (single round after summing); rig/
 * dsp_model_selftest.py proves this form is bit-identical to the vendor's own
 * VPU model (audio_dsp.dsp.biquad.process_xcore) and that the process_int
 * form differs. The same selftest also checks THIS binary against that model,
 * so the chain C == python == vendor model == silicon asm is measured, not
 * assumed.
 *
 * ABI: argv[1] = input file (int32 pairs, L then R, native order), argv[2] =
 * output file (same layout), argv[3..7] = the five int32 coefficients,
 * argv[8] = lsh. int32 (not int64) because the rig holds 23M-frame streams
 * and int64 is what OOM-killed its first version; the rig's arrays and this
 * binary must agree exactly -- rig/dsp_model_selftest.py section 1b is the
 * check that they do. State starts at zero, matching csd_dsp_bq_config's
 * memset at stream start; L and R are independent (the device keeps
 * bq_state[2][8]).
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

static const int32_t I32MIN = (-2147483647 - 1), I32MAX = 2147483647;

static int32_t sat32(int64_t v)
{
    if (v < (int64_t)I32MIN) return I32MIN;
    if (v > (int64_t)I32MAX) return I32MAX;
    return (int32_t)v;
}

/* one product: (x*c + 2^29) >> 30, checked to stay inside int34 the way
 * utils.int34 does -- an escape means the model or the coefficients are
 * wrong, and failing loudly is the only acceptable response. */
/* Product rounding mode, argv[9], for fitting the device's actual rule.
 * DEFAULT 0 keeps the shipped behaviour (the semantics biquad.S's single
 * vlmaccr is modelled with, and what dsp_model_selftest.py pins to the
 * vendor's process_xcore). The others exist so a residual can be attributed
 * by FIT rather than by argument: which rule drives the capture-to-twin
 * difference to zero is the rule the silicon implements. */
static int g_round = 0;

static int64_t prod_round(int64_t raw)
{
    switch (g_round) {
    case 1:                                   /* truncate toward zero */
        return raw >= 0 ? (raw >> 30) : -((-raw) >> 30);
    case 2:                                   /* arithmetic shift (floor) */
        return raw >> 30;
    case 3: {                                 /* round half to even */
        int64_t q = raw >> 30, r = raw & ((1LL << 30) - 1);
        if (r > (1LL << 29) || (r == (1LL << 29) && (q & 1))) q += 1;
        return q;
    }
    default:                                  /* 0: half up -- shipped form */
        return (raw + (1LL << 29)) >> 30;
    }
}

static int64_t mac(int32_t x, int32_t c)
{
    int64_t p = prod_round((int64_t)x * (int64_t)c);
    if (p > ((1LL << 33) - 1) || p < -(1LL << 33)) {
        fprintf(stderr, "dsp_bq_host: VPU product %lld escaped int34 -- "
                        "model or coefficients invalid\n", (long long)p);
        exit(3);
    }
    return p;
}

int main(int argc, char **argv)
{
    if (argc != 9 && argc != 10) {
        fprintf(stderr, "usage: %s IN OUT c0 c1 c2 c3 c4 lsh [round]\n", argv[0]);
        return 2;
    }
    const char *inf = argv[1], *outf = argv[2];
    int32_t c[5];
    for (int i = 0; i < 5; i++) c[i] = (int32_t)strtol(argv[3 + i], NULL, 10);
    int lsh = (int)strtol(argv[8], NULL, 10);
    /* argv[9] selects the product-rounding rule for residual FITTING only.
     * Absent (the rig always omits it) = mode 0, the shipped form that
     * dsp_model_selftest.py pins to the vendor's process_xcore. A fit result
     * from another mode is evidence about the silicon, never a shipping
     * default -- switching the default without the selftest re-passing would
     * silently redefine what "bit-perfect" means. */
    if (argc == 10) g_round = (int)strtol(argv[9], NULL, 10);

    FILE *fi = fopen(inf, "rb");
    if (!fi) { perror("open in"); return 2; }
    FILE *fo = fopen(outf, "wb");
    if (!fo) { perror("open out"); return 2; }

    int32_t buf[2], obuf[2];
    size_t n = 0;
    int32_t st[2][4];                    /* x1 x2 y1 y2, per channel */
    st[0][0]=st[0][1]=st[0][2]=st[0][3]=0;
    st[1][0]=st[1][1]=st[1][2]=st[1][3]=0;

    while (fread(buf, sizeof buf, 1, fi) == 1) {
        for (int ch = 0; ch < 2; ch++) {
            int32_t x = buf[ch];                 /* the device filters int32 */
            int64_t acc = mac(x, c[0])
                        + mac(st[ch][0], c[1])
                        + mac(st[ch][1], c[2])
                        + mac(st[ch][2], c[3])
                        + mac(st[ch][3], c[4]);
            if (acc > (1LL << 39) - 1 || acc < -(1LL << 39)) {
                fprintf(stderr, "dsp_bq_host: accumulator escaped int40\n");
                return 3;
            }
            int32_t y = sat32(acc);              /* vstrpv: saturating store */
            st[ch][1] = st[ch][0];
            st[ch][0] = x;
            st[ch][3] = st[ch][2];
            st[ch][2] = y;
            if (lsh) y = sat32((int64_t)y << lsh);   /* shl sample, lsh */
            obuf[ch] = y;
        }
        if (fwrite(obuf, sizeof obuf, 1, fo) != 1) { perror("write"); return 2; }
        n++;
    }
    fclose(fi); fclose(fo);
    fprintf(stderr, "dsp_bq_host: %zu frames, c=[%d,%d,%d,%d,%d] lsh=%d\n",
            n, c[0], c[1], c[2], c[3], c[4], lsh);
    return 0;
}
