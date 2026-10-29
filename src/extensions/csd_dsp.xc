// CSD PCM in-line DSP — linear-phase bass shelf.
// Phase A3: spine + OFF/DELAY presets; FIR presets arrive with B1.
// Hook: UserBufferManagement() — the lib_xua weak per-frame callback on the
// audiohub core (AN02011/AN02014 documented interception point; precedent:
// lib_dfu app_usb_aud_xk_316_mc user_buffer_management.xc). Single writer:
// all state below is touched only from that core (plus set_* on rate change,
// same core). Compiled empty unless the app defines PCM_DSP_EN=1.
// Coefficient provenance + spec: tools/dsp/gen_coeffs.py --selftest.
#include "xua_defs.h"

#if PCM_DSP_EN

#include "csd_dsp.h"
#include "csd_dsp_coeffs.h"
#include "csd_dsp_core.h"
#include <string.h>

#define CSD_DSP_PRESET_FIR 2u    // presets >= this use filter_fir_s32 (B1)

static struct {
    int32_t ring[2][CSD_DSP_MAX_TAPS];  // history (delay line / FIR state)
    unsigned head;                      // write index
    unsigned n;                         // taps of active table (0 -> bypass)
    unsigned preset;                    // 0 OFF (byte-exact), 1 DELAY-only, >=2 FIR
    unsigned dsd;                       // non-zero -> bypass (stream config)
    unsigned dop;                       // latched in-band DoP signature
    int dopCnt;                         // votes toward DoP
    int dopLeave;                       // votes toward PCM
    int rateIdx;                        // active csd_dsp_rates[] index, -1 none
} csd_dsp;

/* In-band DoP signature: DoP16-in-S32LE carries the marker in bytes 0 and 2
 * of each channel word, alternating 0x05 / 0xFA per frame (lib_xua DoDsdDop
 * uses the same positions). Voting latch: 4 votes to enter (payload must be
 * bit-exact from the first frames, so be quick), 8 contrary votes to leave.
 * Conservative direction of error: false "DoP" = bypass (harmless),
 * missed DoP = corrupt DSD (not acceptable). PCM that fools the checker
 * needs marker bytes + alternation: probability ~2^-16 per frame. */
#define DOP_MARK(s)  ( ((s) & 0xffu) == (((s) >> 16) & 0xffu) && \
                       (((s) & 0xffu) == 0x05u || ((s) & 0xffu) == 0xFAu) )

static unsigned next_head(unsigned h, unsigned n) { h++; return h >= n ? 0u : h; }

static void csd_dsp_restart(void)
{
    csd_dsp.head = 0;
    memset(csd_dsp.ring, 0, sizeof csd_dsp.ring);
}

static void dop_check(unsigned *s)
{
    int match = DOP_MARK(s[0]) && DOP_MARK(s[1]);
    if (match) {
        csd_dsp.dopLeave = 0;
        if (csd_dsp.dopCnt < 4 && ++csd_dsp.dopCnt >= 4 && !csd_dsp.dop) {
            csd_dsp.dop = 1;
            csd_dsp_restart();
        }
    } else {
        csd_dsp.dopCnt = 0;
        if (csd_dsp.dop && ++csd_dsp.dopLeave >= 8) {
            csd_dsp.dop = 0;
            csd_dsp.dopLeave = 0;
            csd_dsp_restart();
        }
    }
}

#pragma unsafe arrays
void csd_dsp_frame(unsigned *s)
{
    /* DoP guard runs regardless of preset: cheap, and keeps detection warm. */
    dop_check(s);

    unsigned n = csd_dsp.n;
    if (n == 0u || csd_dsp.dsd || csd_dsp.dop || csd_dsp.preset == 0u) return;  // byte-exact bypass
    unsigned h = csd_dsp.head;
    if (csd_dsp.preset == 1u) {
        /* DELAY preset: y[t] = x[t-D], D=(n-1)/2 (same delay the FIR applies).
         * Ring size n: the value at index (t-D) mod n was written at t-D and
         * has not been overwritten since. */
        const unsigned D = (n - 1u) / 2u;
        unsigned r = h + n - D;
        if (r >= n) r -= n;
        for (unsigned ch = 0; ch < 2; ch++) {
            int32_t y = csd_dsp.ring[ch][r];      /* 'out' is an XC keyword */
            csd_dsp.ring[ch][h] = (int32_t)s[ch];
            s[ch] = (unsigned)y;
        }
        csd_dsp.head = next_head(h, n);
    } else {
        /* B1: filter_fir_s32 per channel over the Q31 table. Referenced now so
         * the pool survives until the FIR lands; unreachable until then. */
        const csd_dsp_rate_t *t = &csd_dsp_rates[csd_dsp.rateIdx];
        unsigned n_taps = t->num_taps;
        (void) csd_coef_pool[t->coef_ofs];
        (void) n_taps;
    }
}

static int pick_table(unsigned hz)
{
    int best = -1;
    unsigned bestd = 0x7fffffff;
    for (unsigned i = 0; i < CSD_DSP_NUM_RATES; i++) {
        unsigned d = csd_dsp_rates[i].rate > hz ? csd_dsp_rates[i].rate - hz
                                                : hz - csd_dsp_rates[i].rate;
        if (d < bestd) { bestd = d; best = (int)i; }
    }
    /* Refuse a table more than 1 % off (e.g. stream-change to unsupported rate). */
    if (best >= 0 && bestd > (csd_dsp_rates[best].rate / 100u)) best = -1;
    return best;
}

static void csd_dsp_clear_state(void)
{
    csd_dsp.head = 0;
    csd_dsp.n = 0;
    csd_dsp.dop = 0;
    csd_dsp.dopCnt = 0;
    csd_dsp.dopLeave = 0;
    csd_dsp.rateIdx = -1;
    memset(csd_dsp.ring, 0, sizeof csd_dsp.ring);
}

void csd_dsp_set_rate(unsigned hz)
{
    int idx = pick_table(hz);
    csd_dsp_clear_state();
    csd_dsp.rateIdx = idx;
    if (idx >= 0) {
        csd_dsp.n = csd_dsp_rates[idx].num_taps;
    }
}

void csd_dsp_set_dsd(unsigned dsdMode) { csd_dsp.dsd = dsdMode; }

void csd_dsp_set_preset(unsigned p)
{
    if (p == csd_dsp.preset) return;
    if (p >= CSD_DSP_PRESET_FIR && csd_dsp.rateIdx < 0) return;  // no table
    csd_dsp.preset = p;
    csd_dsp_restart();                       // avoid pop from stale state
}

/* A3 test builds force the DELAY preset with -DCSD_DSP_PRESET_DEFAULT=1
 * (button/host selection arrives with B3). */
#ifdef CSD_DSP_PRESET_DEFAULT
void csd_dsp_preset_boot(void) { csd_dsp_set_preset(CSD_DSP_PRESET_DEFAULT); }
#else
void csd_dsp_preset_boot(void) { }
#endif

unsigned csd_dsp_get_preset(void) { return csd_dsp.preset; }

/* lib_xua weak hook, overridden here (see header comment). Runs on the
 * audiohub core at stream rate, once per frame, before I2S out. */
#pragma unsafe arrays
void UserBufferManagement(unsigned sampsFromUsbToAudio[],
                          unsigned sampsFromAudioToUsb[])
{
    /* Output path only: sampsFromAudioToUsb (ADC->USB) passes through. */
    csd_dsp_frame(sampsFromUsbToAudio);
}

#endif // PCM_DSP_EN
