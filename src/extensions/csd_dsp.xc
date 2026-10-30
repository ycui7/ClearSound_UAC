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
#include <stdint.h>
#ifdef CSD_DSP_DIAG_SLOTS
#include <stdio.h>
#endif

/* FIR engine: XMOS lib_audio_dsp fd_block_fir (AN02027) — frequency-domain
 * block convolution, ~6x cheaper than the single-sample VPU kernel. Blocking
 * adapter lives in csd_dsp_fd.c (same core, called from the frame hook).
 *
 * Measured on this board: one 128-sample block costs ~11,900 reference-time
 * ticks per channel, while the audiohub thread must hand a frame to the I2S
 * ports every LRCLK (~1560 ticks at 384 kHz). The burst trips HandleSampleClock
 * -> syncError and the streaming loop exits, so the block engine cannot live
 * inline on this thread at high rate. Default engine is therefore the vendor
 * biquad (O(1)/sample, no burst); build with -DCSD_DSP_ENGINE_FD=1 to restore
 * the block path for offline experiments. */
void csd_dsp_fd_config(int rate_idx);
void csd_dsp_fd_clear(void);
int  csd_dsp_fd_ready(void);
void csd_dsp_fd_frame(unsigned s[2]);   // same element type as the lib_xua hook

/* Bass shelf via lib_audio_dsp adsp_biquad (VPU asm): [b0,b1,b2,-a1,-a2] in
 * Q(1+lsh).30, coefficients from tools/dsp/gen_biquad.py. */
void csd_dsp_bq_config(int rate_idx);
void csd_dsp_bq_clear(void);
int  csd_dsp_bq_ready(void);
void csd_dsp_bq_frame(unsigned s[2]);

#define CSD_DSP_PRESET_FIR 2u    // presets >= this apply the bass shelf

/* Engine selection. Default = vendor biquad (VPU asm, O(1) per sample, no
 * burst -> safe on the audiohub thread). CSD_DSP_ENGINE_FD=1 restores the
 * fd_block_fir block path for offline experiments; it is known to trip
 * HandleSampleClock at high rates when called inline here. */
#ifdef CSD_DSP_ENGINE_FD
#define CSD_ENGINE_CONFIG(i) csd_dsp_fd_config(i)
#define CSD_ENGINE_CLEAR()   csd_dsp_fd_clear()
#define CSD_ENGINE_READY()   csd_dsp_fd_ready()
#define CSD_ENGINE_FRAME(s)  csd_dsp_fd_frame(s)
#else
#define CSD_ENGINE_CONFIG(i) csd_dsp_bq_config(i)
#define CSD_ENGINE_CLEAR()   csd_dsp_bq_clear()
#define CSD_ENGINE_READY()   csd_dsp_bq_ready()
#define CSD_ENGINE_FRAME(s)  csd_dsp_bq_frame(s)
#endif

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
    /* Re-arm the FIR for the active preset; never leave the FIR preset
     * silently unfiltered (no degraded-mode bypass). */
    if (csd_dsp.preset >= CSD_DSP_PRESET_FIR && csd_dsp.rateIdx >= 0)
        CSD_ENGINE_CONFIG(csd_dsp.rateIdx);
    else
        CSD_ENGINE_CLEAR();
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
    /* FIR preset: bass shelf. Default engine is the vendor biquad (VPU asm,
     * O(1) per sample, no burst). The fd block engine sits behind
     * CSD_DSP_ENGINE_FD -- measured ~11.9k ticks per 128-sample block per
     * channel blows the per-LRCLK budget (~1.56k ticks at 384 kHz) and trips
     * HandleSampleClock -> syncError, which stops the audiohub loop. */
        if (!CSD_ENGINE_READY()) return;
        CSD_ENGINE_FRAME(s);
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
    /* IDEMPOTENT: the board's AudioHwConfig is re-invoked repeatedly while a
     * stream is live (console shows config every few hundred ms). A teardown on
     * every call resets fd_idx and memsets the block state, so the FD engine
     * never accumulates FRAME_ADVANCE samples: output stays zero. Identical
     * rate => keep running state, touch nothing. */
    if (idx >= 0 && idx == csd_dsp.rateIdx && csd_dsp.n == csd_dsp_rates[idx].num_taps)
        return;
    csd_dsp_clear_state();
    csd_dsp.rateIdx = idx;
    if (idx >= 0) {
        csd_dsp.n = csd_dsp_rates[idx].num_taps;
        if (csd_dsp.preset >= CSD_DSP_PRESET_FIR)
            CSD_ENGINE_CONFIG(idx);   /* same-core writer; no lock needed */
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

#ifdef CSD_DSP_DIAG
/* Device-execution proof, diagnostics only: the mic path is independent of the
 * DAC, so stamping it from inside the hook distinguishes "hook ran, kernel
 * output zero" from "audiohub thread died" -- both look like DAC silence. */
static unsigned csd_diag_frames = 0;
#ifdef CSD_DSP_DIAG_SELFTEST
extern unsigned csd_dsp_fd_selftest(unsigned rate_idx);
static unsigned csd_diag_selftest = 0xDEADu;
#endif
int csd_dsp_diag_state(void) { return (int)csd_dsp.preset; }
unsigned csd_dsp_diag_count(void) { return csd_diag_frames; }
#endif

/* lib_xua weak hook, overridden here (see header comment). Runs on the
 * audiohub core at stream rate, once per frame, before I2S out. */
#pragma unsafe arrays
void UserBufferManagement(unsigned sampsFromUsbToAudio[],
                          unsigned sampsFromAudioToUsb[])
{
#ifdef CSD_DSP_DIAG_SLOTS
    /* Where does the audio actually sit in the USB->DAC buffer at hook entry?
     * Print a slot census every 50k calls (~130 ms at 384 kHz) so the census
     * lands DURING a live tone -- a stream-start print sees only pre-stream
     * zeros and proves nothing. */
    if ((csd_dsp_diag_count() % 50000u) == 12345u) {
        printf("SLOTS n=%u [%d,%d,%d,%d,%d,%d,%d,%d] out [%d,%d]\n",
               csd_dsp_diag_count(),
               (int)sampsFromUsbToAudio[0], (int)sampsFromUsbToAudio[1],
               (int)sampsFromUsbToAudio[2], (int)sampsFromUsbToAudio[3],
               (int)sampsFromUsbToAudio[4], (int)sampsFromUsbToAudio[5],
               (int)sampsFromUsbToAudio[6], (int)sampsFromUsbToAudio[7],
               (int)sampsFromAudioToUsb[0], (int)sampsFromAudioToUsb[1]);
    }
#endif
#ifdef CSD_DSP_DIAG_SELFTEST
    /* Run once on the FIRST hook invocation -- i.e. on the audiohub core, the
     * exact context that fails -- and report the adapter's own gain for a
     * known sine. gain~1.0 with garbage live audio => fault is in integration
     * (state ownership/timing); gain~0 => context-dependent kernel failure. */
    static int csd_st_ran = 0;
    if (!csd_st_ran && csd_dsp_diag_state() == 2) {
        csd_st_ran = 1;
        csd_diag_selftest = csd_dsp_fd_selftest(1u /* 48k index */);
    }
#endif
#ifdef CSD_DSP_DIAG
    csd_diag_frames++;
    /* Diagnostics ride the MIC path (proven to reach the host intact), so no
     * console is needed:
     *   L = hook magic (low bits lost in the 24-bit capture path)
     *   R = preset<<24 | frame count   -> proves which branch was taken
     * With CSD_DSP_DIAG_SP the pair instead encodes the live frame pointer of
     * this invocation: L = low 24 bits, R = preset<<24 | (fp >> 24). Reassembling
     * them locates the audiohub thread's stack relative to our .bss statics
     * (fd_blob 0x919c8 .. fd_out/fd_in ~0x98014), i.e. detects stack collision
     * with the DSP state -- a silent-corruption failure mode no assert catches. */
#ifdef CSD_DSP_DIAG_SP
    unsigned fp = (unsigned)__builtin_frame_address(0);
#endif
#ifdef CSD_DSP_DIAG_DOP
    /* R = preset<<24 | dop<<16 | (frame counter & 0xFFFF). The dop BIT is the
     * suspect: a false-positive DoP latch (DOP_MARK fires ~2^-16/frame on
     * audio-shaped data, and 4 votes latch) makes csd_dsp_frame RETURN EARLY --
     * byte-exact bypass -- which would look exactly like "FD branch runs but
     * emits noise" if the preset stamp alone is believed. */
    sampsFromAudioToUsb[0] = 0x01020304u;
    sampsFromAudioToUsb[1] = ((unsigned)csd_dsp_diag_state() << 24) |
                             ((unsigned)(csd_dsp.dop ? 1u : 0u) << 16) |
                             (csd_dsp_diag_count() & 0x0000FFFFu);
#else
    sampsFromAudioToUsb[0] = 0x01020304u;
    sampsFromAudioToUsb[1] = ((unsigned)csd_dsp_diag_state() << 24) |
                             (csd_dsp_diag_count() & 0x00FFFFFFu);
#endif
#ifdef CSD_DSP_DIAG_SELFTEST
    /* L carries the adapter selftest result for the whole run (constant):
     * L = (lag<<12)|matches, matches saturated at 4095 => bit-exact delayed
     * copy => FD adapter+kernel works IN THIS CONTEXT (audiohub core).
     * matches==0 => kernel produces nothing here. 0xDEAD => never ran. */
    if (csd_diag_selftest != 0xDEADu)
        sampsFromAudioToUsb[0] = csd_diag_selftest;
#endif
#ifdef CSD_DSP_DIAG_SP
    sampsFromAudioToUsb[0] = fp & 0x00FFFFFFu;
    sampsFromAudioToUsb[1] = ((unsigned)csd_dsp_diag_state() << 24) |
                             ((fp >> 24) & 0x00FFFFFFu);
#endif
#endif
#ifdef CSD_DSP_DIAG_RAW
    /* L = raw USB->DAC sample BEFORE any DSP, R = the same sample AFTER the
     * engine (post-DSP, exactly what goes to I2S). One capture answers both
     * "is the engine fed a proper sine" and "what does it emit". Preset stamp
     * is sacrificed: preset is compiled-in (2) and already proven. */
    unsigned csd_pre0 = sampsFromUsbToAudio[0];
#endif
    /* Output path only: sampsFromAudioToUsb (ADC->USB) passes through. */
#ifdef CSD_DSP_DIAG_RAW
    sampsFromAudioToUsb[0] = csd_pre0;
#endif
    csd_dsp_frame(sampsFromUsbToAudio);
#ifdef CSD_DSP_DIAG_RAW
    sampsFromAudioToUsb[1] = sampsFromUsbToAudio[0];
#endif
#ifdef CSD_DSP_DIAG_LOOP
    /* csd_dsp_frame rewrote the buffer IN PLACE, so it now holds exactly the
     * sample that goes to I2S/DAC. Mirror it onto the mic path: this measures
     * kernel output through the proven-good capture path, with no second pass
     * (a second call would double-advance the block cadence and corrupt the
     * very thing being measured). */
    sampsFromAudioToUsb[0] = sampsFromUsbToAudio[0];
    sampsFromAudioToUsb[1] = sampsFromUsbToAudio[1];
#endif
}

#endif // PCM_DSP_EN
