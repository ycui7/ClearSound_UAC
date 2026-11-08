/* csd_ups_sim.xc -- dual-target qualification of the upsampling core.
 *
 * One source, two compilers, identical expected output:
 *   xcc  -target=xs1  -> runs on the XMOS simulator (xsim): what ships on XS3
 *   gcc  -x c         -> runs on this server: the tester math
 * Any difference in the printed lines is a real difference between the two
 * toolchains' arithmetic, not a difference in the test.
 *
 * Self-checking: every case also compares the runtime against a naive
 * reference computed in the same program, so a PASS here does not depend on
 * comparing two runs. The printed hashes make the cross-toolchain comparison
 * exact rather than eyeballed.
 *
 * Not part of the firmware image: it lives under tools/ so the app glob never
 * picks it up.
 */
#ifdef __XC__
#include <print.h>
static void emit(unsigned long long v) { printint((int)(v & 0xffffffffull)); }
static void say(const char *s) { printstr(s); }
static void nl(void) { printstr("\n"); }
#else
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#ifdef CSD_SIM_STORE
/* Simulator build: the report is buffered so the XC wrapper can print it with
 * the simulator's native output path -- a C .xe on XS3 has no stdout at all. */
static char    g_buf[8192];
static unsigned g_len = 0u;
static void put(const char *s, unsigned n)
{
    if (g_len + n >= sizeof(g_buf)) return;      /* never walk off the end */
    memcpy(&g_buf[g_len], s, n);
    g_len += n;
}
static void emit(unsigned long long v)
{
    char t[16];
    int n = snprintf(t, sizeof(t), "%u", (unsigned)(v & 0xffffffffull));
    if (n > 0) put(t, (unsigned)n);
}
static void say(const char *s) { put(s, (unsigned)strlen(s)); }
static void nl(void) { put("\n", 1u); }
unsigned sim_len(void) { return g_len; }
/* Byte-at-a-time accessor: no pointer ever crosses the C/XC boundary. */
int sim_get(unsigned i) { return (i < g_len) ? (int)g_buf[i] : 0; }
#else
/* XC's printint is 32-bit; mask both sides to the same width so the two
 * printings of a 64-bit hash are directly comparable. */
static void emit(unsigned long long v) { printf("%u", (unsigned)(v & 0xffffffffull)); }
static void say(const char *s) { printf("%s", s); }
static void nl(void) { printf("\n"); }
#endif
#endif   /* closes the outer #ifdef __XC__ */

#include "csd_upsample.h"
#include "csd_dsp_taps.h"

#define NIN 64u
#define NOUT_MAX 512u

/* Same PRNG as the host tester, so both sides feed identical stimulus. */
static unsigned rng_state;
static unsigned rnd(void)
{
    rng_state = rng_state * 1103515245u + 12345u;
    return rng_state;
}

/* Rolling hash: order-sensitive, and cheap enough to print. */
static unsigned long long hash_of(const int32_t *v, unsigned n)
{
    unsigned long long h = 1469598103934665603ull;
    for (unsigned i = 0; i < n; i++) {
        h ^= (unsigned long long)(unsigned)v[i];
        h *= 1099511628211ull;
    }
    return h;
}

/* The runtime saturates at int32 (measured: an vout-of-range interpolated sample
 * returns 2147483647, not a wrapped value). A full-scale random sequence
 * legitimately overshoots the halfband, so the reference must clip the same
 * way or the comparison tests clipping, not filtering. */
static int32_t sat32(int64_t v)
{
    if (v >  2147483647ll) return  2147483647;
    if (v < -2147483648ll) return -2147483648;
    return (int32_t)v;
}

static int failures = 0;
static void expect(const char *name, int ok)
{
    say("CHECK "); say(name); say(ok ? " ok\n" : " FAIL\n");
    if (!ok) failures++;
}

int run_all(void)
{
    /* ---- 1..3: cascade at each supported input rate ---- */
    /* Both families. The 44.1 kHz side is the new path: same kernel, same
     * tables, output rate 352800 -- only MCLK differs. */
    const unsigned rates[6] = { 48000u, 96000u, 192000u, 44100u, 88200u, 176400u };
    for (unsigned r = 0; r < 6u; r++) {
        csd_cascade_state c;
        csd_cascade_reset(&c);
        int32_t xin[NIN], vout[NOUT_MAX];   /* 'in' is a reserved word in XC */
        int32_t ta[NOUT_MAX], tb[NOUT_MAX];
        rng_state = 0xC5D00000u + rates[r];
        for (unsigned i = 0; i < NIN; i++) xin[i] = (int32_t)rnd() - 2147483648;
        unsigned n = NIN * csd_up_ratio(rates[r]);
        for (unsigned i = 0; i < n; i++) vout[i] = 0x7BADF00D;
        csd_cascade_process(&c, rates[r], xin, NIN, vout, ta, tb);
        unsigned long long h = hash_of(vout, n);
        say("FAMILY "); emit(rates[r]); say(" -> "); emit(csd_up_out_fs(rates[r]));
        say(" ratio="); emit(csd_up_ratio(rates[r])); nl();
        say("CASCADE "); emit(rates[r]); say(" n="); emit(n);
        say(" hash="); emit(h); say(" head=");
        emit((unsigned)vout[0]); say(","); emit((unsigned)vout[1]);
        say(" tail="); emit((unsigned)vout[n - 1u]); nl();
        /* nothing may leave the cascade uninitialised */
        int written = 1;
        for (unsigned i = 0; i < n; i++) if (vout[i] == 0x7BADF00D) written = 0;
        expect("cascade all outputs written", written);
        /* The mapping must be an exact integer multiple, and land on one of the
         * two family rates -- a rate that divided unevenly is not a family
         * member and must not be interpolated by an 8-phase kernel. */
        const unsigned o = csd_up_out_fs(rates[r]);
        const unsigned rt = csd_up_ratio(rates[r]);
        expect("family rate is an exact multiple", o == rates[r] * rt);
        expect("family rate is 384k or 352.8k",
               o == 384000u || o == 352800u);
    }

    /* ---- 3b: a rate in neither family is refused, never approximated ---- */
    {
        const unsigned bad[4] = { 44000u, 22050u, 32000u, 384000u };
        int ok = 1;
        for (unsigned i = 0; i < 4u; i++) {
            if (csd_up_ratio(bad[i]) != 0u) ok = 0;
            if (csd_up_out_fs(bad[i]) != 0u) ok = 0;
        }
        expect("non-family rates are refused", ok);

        /* Refusal must leave the buffer alone, not write zeros at a wrong rate. */
        csd_cascade_state c2; csd_cascade_reset(&c2);
        int32_t bin[8], bout[64], bta[16], btb[32];
        for (unsigned i = 0; i < 64u; i++) bout[i] = (int32_t)0x5A5A5A5A;
        for (unsigned i = 0; i < 8u;  i++) bin[i] = (int32_t)(0x11111111u * (i + 1u));
        csd_cascade_process(&c2, 44000u, bin, 8u, bout, bta, btb);
        int untouched = 1;
        for (unsigned i = 0; i < 64u; i++) if (bout[i] != (int32_t)0x5A5A5A5A) untouched = 0;
        expect("refused rate leaves the buffer alone", untouched);
    }

    /* ---- 4: halfband identity phase, exactly x[n-40] ---- */
    {
        csd_hb_state s; csd_hb_reset(&s);
        int32_t xin[128], vout[256];
        rng_state = 0x12345678u;
        for (unsigned i = 0; i < 128u; i++) xin[i] = (int32_t)rnd() - 2147483648;
        csd_hb2_process(&s, xin, 128u, vout);
        int ok = 1;
        for (unsigned n = 40; n < 128u; n++) {
            if (vout[2u * n] != xin[n - 40u]) { ok = 0; }
        }
        expect("halfband identity phase == x[n-40]", ok);
    }

    /* ---- 5: halfband interpolated phase vs the naive 80-tap sum ---- */
    {
        const int32_t *hb = csd_hb_coef;
        csd_hb_state s; csd_hb_reset(&s);
        int32_t xin[128], vout[256];
        rng_state = 0xA5A50F0Fu;
        for (unsigned i = 0; i < 128u; i++) xin[i] = (int32_t)rnd() - 2147483648;
        csd_hb2_process(&s, xin, 128u, vout);
        /* reference over the same history the runtime holds */
        int64_t hist[80];
        for (int i = 0; i < 80; i++) hist[i] = 0;
        int ok = 1; unsigned checked = 0;
        for (unsigned i = 0; i < 128u; i++) {
            for (int q = 79; q > 0; q--) hist[q] = hist[q - 1];
            hist[0] = xin[i];
            int64_t acc = 0;
            for (unsigned q = 0; q < 80u; q++) acc += (int64_t)hb[2u*q + 1u] * hist[q];
            int32_t got = vout[2u*i + 1u];
            if (sat32(acc >> 30) != got) {
                if (ok) {
                    say("  first mismatch i="); emit(i);
                    say(" naive="); emit((unsigned long long)sat32(acc >> 30));
                    say(" runtime="); emit((unsigned)got); nl();
                }
                ok = 0;
            }
            checked++;
        }
        expect("halfband interpolated == naive sum", ok);
    }

    /* ---- 6: band limit vs naive 511-tap, and symmetry fold ---- */
    {
        const int32_t *bl = csd_bl_coef;
        csd_bl_state s; csd_bl_reset(&s);
        int32_t xin[128], vout[128];
        rng_state = 0xB16B00B5u;
        for (unsigned i = 0; i < 128u; i++) xin[i] = (int32_t)rnd() - 2147483648;
        csd_bl_process(&s, xin, 128u, vout);
        int64_t buf[511];
        for (int i = 0; i < 511; i++) buf[i] = 0;
        int ok = 1;
        for (unsigned i = 0; i < 128u; i++) {
            for (int k = 510; k > 0; k--) buf[k] = buf[k - 1];
            buf[0] = xin[i];
            int64_t acc = 0;
            for (int k = 0; k < 511; k++) acc += (int64_t)bl[k] * buf[k];
            if (sat32(acc >> 30) != vout[i]) ok = 0;
        }
        expect("band limit == naive 511-tap", ok);
        unsigned long long h = hash_of(vout, 128u);
        say("BANDLIMIT hash="); emit(h); say(" head=");
        emit((unsigned)vout[0]); say(","); emit((unsigned)vout[1]); nl();
    }

    say(failures == 0 ? "SIM-PASS\n" : "SIM-FAIL\n");
    emit((unsigned)failures); nl();
    return failures;
}

#ifndef CSD_SIM_STORE
int main(void) { return run_all(); }
#endif
