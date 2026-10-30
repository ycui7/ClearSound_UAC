/* Audio measurement probes -- see csd_meas.h for the contract.
 *
 * Writer runs on the audiohub core (LRCLK-critical), so the per-sample work is
 * deliberately tiny and integer-only: shift, one 32-bit multiply, two adds, a
 * compare, and a hysteresis test, per channel. The square root and the
 * divisions live on the reader side (diagnostic core) where latency does not
 * matter.
 *
 * Scaling: q = x>>16 keeps |q| <= 32767, so q*q fits a 32-bit word and the
 * accumulate is a cheap 32x32 multiply plus a 64-bit add -- no soft-float, no
 * 64-bit multiply on the audio thread. */
#include <stdint.h>
#include <string.h>
#include "csd_meas.h"

#define WINDOW   CSD_MEAS_WINDOW
#define HYST     (1 << 24)          /* ~ -12 dBFS: below this a "crossing" is
                                     * noise, so frequency is reported as 0. */

static volatile csd_meas_snap_t snap[2];
static volatile unsigned active;                 /* index reader should use */
static csd_meas_ch_t acc[CSD_MEAS_CH];
static unsigned filled;                          /* samples in current window */
static uint64_t sumsq[CSD_MEAS_CH];
static uint32_t hz_pub;

void csd_meas_reset(void)
{
    memset((void *)acc, 0, sizeof acc);
    memset((void *)sumsq, 0, sizeof sumsq);
    memset((void *)snap, 0, sizeof snap);
    filled = 0;
    active = 0;
}

void csd_meas_set_rate(uint32_t hz) { hz_pub = hz; }

static inline void feed_one(unsigned ch, int32_t x)
{
    int32_t q = x >> 16;                     /* |q| <= 32767 */
    sumsq[ch] += (uint64_t)((uint32_t)(q * q));

    uint32_t m = (x < 0) ? (uint32_t)(-(int64_t)x) : (uint32_t)x;
    if (m > acc[ch].peak) acc[ch].peak = m;

    /* zero-crossing with hysteresis, both edges must exceed HYST */
    if (x > (int32_t)HYST) {
        if (acc[ch].last_state != 1) { acc[ch].last_state = 1; acc[ch].crossings++; }
    } else if (x < -(int32_t)HYST) {
        if (acc[ch].last_state != -1) { acc[ch].last_state = -1; acc[ch].crossings++; }
    }

    if (++filled == WINDOW) {
        unsigned nxt = active ^ 1u;                  /* write the idle slot */
        for (unsigned c = 0; c < CSD_MEAS_CH; c++) {
            /* mean square of this window; >>8 keeps it well inside 32 bits
             * (max 2^38 / 256 = 2^30) and preserves ~8 bits of headroom */
            csd_meas_ch_t *d = (csd_meas_ch_t *)&snap[nxt].ch[c];
            d->sumsq     = (uint32_t)(sumsq[c] >> 8);
            d->peak      = acc[c].peak;
            d->crossings = acc[c].crossings;
            d->frames    = acc[c].frames + 1u;
            d->last_state= acc[c].last_state;
            d->valid     = 1u;
            sumsq[c] = 0;
            acc[c].peak = 0;
            acc[c].crossings = 0;
        }
        ((csd_meas_snap_t *)&snap[nxt])->hz = hz_pub;
        active = nxt;                                /* publish: single store */
        filled = 0;
    }
}

void csd_meas_out(int32_t l, int32_t r)
{
    feed_one(0, l);
    feed_one(1, r);
}

void csd_meas_in(int32_t l, int32_t r)
{
    feed_one(2, l);
    feed_one(3, r);
}

/* Reader: returns the published snapshot, or NULL until a window completes.
 * Re-checks the index afterwards so a torn read is reported as "stale" (NULL)
 * rather than answered with a mixed old/new number. */
const csd_meas_snap_t *csd_meas_snapshot(void)
{
    unsigned i = active;
    const csd_meas_snap_t *s = &snap[i];
    if (!s->ch[0].valid) return NULL;
    if (active != i) return NULL;                    /* republished mid-read */
    return s;
}

/* integer square root (reader side only; ~16 iterations, no libm) */
static uint32_t isqrt_u32(uint32_t v)
{
    uint32_t r = 0, b = 1u << 30;
    while (b > v) b >>= 2;
    while (b) {
        if (v >= r + b) { v -= r + b; r = (r >> 1) + b; }
        else            { r >>= 1; }
        b >>= 2;
    }
    return r;
}

uint32_t csd_meas_rms_q31(const csd_meas_ch_t *c)
{
    if (!c->valid) return 0;
    /* sumsq was stored as (x>>16)^2 summed then >>8; mean over the window
     * then sqrt gives an r.m.s. in q-units, shift back to Q31. */
    uint32_t mean = c->sumsq / WINDOW;               /* <= 2^30 >> 8 */
    return isqrt_u32(mean << 8) << 16;
}

uint32_t csd_meas_freq_hz(const csd_meas_ch_t *c, uint32_t hz)
{
    if (!c->valid || c->crossings < 4u || hz == 0u) return 0;
    /* crossings counts both edges: a full cycle is two crossings */
    return (c->crossings / 2u) * hz / WINDOW;
}
