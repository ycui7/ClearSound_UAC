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
#ifdef CSD_GEN_EN
/* Interface only. csd_gen_core.h -- which carries the ~20 KB generated tables
 * as file-scope statics -- is included by exactly one translation unit,
 * csd_gen.xc; including it here as well would emit a second copy into a
 * tile's 512 KB of reserved memory. See the header comment in csd_gen.h. */
#include "csd_gen.h"
#endif
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

/* Adopt a posted preset on the audio thread. Defined with the posting side
 * below; needed by both audio-thread entry points (set_rate, frame hook). */
static void csd_dsp_apply_pending_preset(void);

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

#if !(defined(CSD_GEN_EN) && defined(CSD_GEN_PREMIX))
/* Post-mixer detector. In a pre-mixer build the mixer thread votes instead,
 * on the RAW host pair before the tone is summed in (see csd_gen_mix_begin),
 * so this would be dead code -- and dead code in a per-frame path is how a
 * stale detector survives to disagree with the live one. Guarded rather than
 * deleted: the non-mixer build still needs it, and the gate that proves DSD
 * suppression was measured on that build. */
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
#endif

#pragma unsafe arrays
void csd_dsp_frame(unsigned *s)
{
    /* DoP guard: vote on whatever the host sent. Runs regardless of preset
     * (cheap, keeps detection warm) and regardless of the generator -- but
     * only on OFF, never on generator samples. The comment near
     * CSD_DSP_DIAG_DOP records that DOP_MARK fires on roughly 1 frame in
     * 65536 of audio-shaped data and that four votes latch the flag: a
     * generator driving full-scale samples continuously is precisely a long
     * run of audio-shaped data, so voting on ITS samples would latch DoP as
     * an expected event, not a tail risk. While the generator is armed the
     * frame hook votes on the RAW host frame instead -- before injection
     * overwrites it -- so a genuine DoP stream is detected and the
     * generator's own wave never votes.
     *
     * No "armed: clear the latch" branch any more. It existed while
     * injection could not be gated; now the leave vote (eight non-matching
     * RAW frames, <=8 frames) unwinds a stale latch while the dop flag
     * ALREADY suppresses injection, so a false latch cannot corrupt audio --
     * and keeping the clear would erase the legitimate DoP latch the hook
     * votes every frame, undoing the suppression this path exists for. */
#ifdef CSD_GEN_PREMIX
    /* Mixer build: the mixer thread voted on the RAW host pair before the
     * tone was summed in, and that verdict is the one the DSP bypass must
     * honour. Re-voting here would read tone + host, which is how a loud
     * full-scale sweep latches DoP as an expected event.
     *
     * The edge check is not decoration. The post-mixer detector restarts the
     * engine on every latch transition; adopting a verdict silently would
     * leave the FIR ring holding samples from before a mid-stream DoP window
     * and replay them into the PCM that follows. */
    {
        unsigned d = csd_gen_mix_dop();
        if (d != csd_dsp.dop) {
            csd_dsp.dop = d;
            csd_dsp_restart();
        }
    }
#elif defined(CSD_GEN_EN)
    if (csd_gen_state_mode() == CSD_GEN_MODE_OFF)
    {
        dop_check(s);
    }
#else
    {
        dop_check(s);
    }
#endif

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

    /* Honour a preset change made while no stream was running. This is the
     * other audio-thread entry point (called from AudioHwConfig at every
     * stream start); the frame hook only runs while a stream is live, so
     * without this a preset posted during idle would never be adopted. */
    csd_dsp_apply_pending_preset();

    /* IDEMPOTENT: the board's AudioHwConfig is re-invoked repeatedly while a
     * stream is live (console shows config every few hundred ms). A teardown on
     * every call resets fd_idx and memsets the block state, so the FD engine
     * never accumulates FRAME_ADVANCE samples: output stays zero. Identical
     * rate => keep running state, touch nothing. */
    if (idx >= 0 && idx == csd_dsp.rateIdx && csd_dsp.n == csd_dsp_rates[idx].num_taps)
        return;

#ifdef CSD_GEN_EN
    /* Rate actually changed: the generator derives its phase steps from fs, so
     * it has to be told or "1 Hz to 20 kHz in 30 s" silently becomes a 15 s
     * sweep at 96 kHz. Called here, on the audio thread, which is the only
     * thread that touches the generator's live state -- which is what lets it
     * be lock-free. Placed after the idempotence check so the repeated
     * AudioHwConfig calls during a live stream do not restart the wave. */
#ifdef CSD_GEN_PREMIX
    /* The mixer thread owns csd_gen in a mixer build. Leave the rate as a
     * single word for it to collect at its frame boundary; calling
     * csd_gen_notify_rate() here would edit the live descriptor from a
     * thread that does not own it. */
    csd_gen_mix_set_rate(hz);
#else
    csd_gen_notify_rate(hz);
#endif
#endif
    csd_dsp_clear_state();
    csd_dsp.rateIdx = idx;
    if (idx >= 0) {
        csd_dsp.n = csd_dsp_rates[idx].num_taps;
        if (csd_dsp.preset >= CSD_DSP_PRESET_FIR)
            CSD_ENGINE_CONFIG(idx);   /* same-core writer; no lock needed */
    }
}

void csd_dsp_set_dsd(unsigned dsdMode) { csd_dsp.dsd = dsdMode; }

/* Raw views for GENerator:COUNts?: the rig distinguishes "gate suppressed a
 * frame because the STREAM is DSD" from "because WE latched DoP", and
 * distinguishes a stuck latch from a live one. Plain reads of audio-thread
 * state: informational, never used for control. */
unsigned csd_dsp_diag_dsd_flag(void)     { return csd_dsp.dsd; }
unsigned csd_dsp_diag_dop_latched(void)  { return csd_dsp.dop ? 1u : 0u; }

/* Preset requests from the diagnostic thread.
 *
 * The MIDI diagnostic task and the audiohub thread are DIFFERENT THREADS
 * (measured 2026-10-30: the audio thread's frame counter advanced 2.09M ->
 * 2.53M while its view of csd_dsp.preset stayed frozen at the boot default,
 * even though the diagnostic thread's readback reported the new value
 * correctly -- see workspace/csd-diag stamp_test.sh). Whatever resolves that
 * split, the rule below is the one that is safe under it.
 *
 * The audio thread owns csd_dsp.preset: it is the only writer. The diagnostic
 * thread only posts requests. This is stricter than the previous design, which
 * published the preset word directly from the diagnostic thread on the theory
 * that an aligned word store cannot be torn. Torn or not, the reader can still
 * never see it: XC's single-writer model lets xcc cache the load of a global
 * across the blocking calls in the streaming loop (observed directly once --
 * disassembly of a plain-global flag showed the store and no load at all).
 * A store the reader has hoisted away is just as invisible as a torn one.
 *
 * Every field that crosses threads is therefore read/written through the
 * volatile helpers below, which force a memory access on the XCore, and only
 * the audio thread mutates DSP state.
 *
 * csd_dsp_bump exists to make the ownership question measurable from the host:
 * the diagnostic thread increments it on every set-preset command, and under
 * CSD_DSP_DIAG_BUMP the audio thread stamps it into the mic-path L channel.
 * Resolved 2026-10-30 by this instrument: the host saw the bump advance
 * 1->2->3 across three preset commands, so the two threads DO share memory
 * (the diagnostic task is not on its own tile), and the frozen preset was
 * therefore a LOGIC bug -- not a cache/coherency problem and not a hoisted
 * load. The actual fault is documented at csd_dsp_preset_boot().
 *
 * Keep the bump. It is the only cheap way to distinguish "threads do not share
 * memory" from "threads share memory but disagree about a value", and those
 * two symptoms are otherwise indistinguishable from the host -- I spent a long
 * stretch assuming the former while it was the latter.
 *
 * The volatile accessors below are retained on correctness grounds: XC's
 * single-writer model genuinely does permit xcc to cache a global's load
 * across the blocking calls in the streaming loop, and a cross-thread global
 * is exactly the case where that is not allowed. They were necessary, not
 * sufficient, and they were NOT the fix -- do not remove them on the theory
 * that they were tried and failed. */
#define CSD_DSP_PRESET_NONE (0xFFFFFFFFu)
/* The compile-time default IS the initial request, not something re-posted at
 * stream start -- see csd_dsp_preset_boot(). */
#ifdef CSD_DSP_PRESET_DEFAULT
#define CSD_DSP_PRESET_BOOT CSD_DSP_PRESET_DEFAULT
#else
#define CSD_DSP_PRESET_BOOT 0u
#endif
unsigned csd_dsp_preset_req = CSD_DSP_PRESET_BOOT;    /* host-requested preset */
unsigned csd_dsp_bump = 0;                            /* command counter      */

/* volatile-through-unsafe-pointer is the documented XCore way to defeat
 * register caching: XC accepts `volatile` only on unsafe pointer targets, and
 * `shared` only on variables declared inside a par block, so a plain global
 * with a plain read is the one combination the compiler is free to optimize
 * away. Cast and deref must both sit inside the unsafe region. */
static unsigned csd_vol_load(unsigned *v)
{
    unsigned x;
    unsafe { x = *(volatile unsigned * unsafe)v; }
    return x;
}
static void csd_vol_store(unsigned *v, unsigned x)
{
    unsafe { *(volatile unsigned * unsafe)v = x; }
}

void csd_dsp_set_preset(unsigned p)
{
    /* Diagnostic-thread side: post only. Never touches csd_dsp. */
    csd_vol_store(&csd_dsp_preset_req, p);
    /* Read and write the counter in separate statements. The one-expression
     * form (store(x, load(x)+1)) is rejected outright: "use of csd_dsp_bump
     * causes an ambiguous evaluation". XC refuses to leave a same-object
     * read/write pair inside one expression, where C would compile it and
     * assign the ordering to chance -- take the hint and sequence it. */
    unsigned b = csd_vol_load(&csd_dsp_bump);
    csd_vol_store(&csd_dsp_bump, b + 1u);
}

/* Adopt a posted preset. Called from the audio thread only, at points where
 * no sample is mid-computation: the top of the frame hook and stream (re-)
 * configuration (the hook runs only while a stream is live, so a command
 * posted during idle must also be picked up here at stream start -- that was
 * the bug behind "PRESet accepted, PRESet? answers boot default forever"). */
static void csd_dsp_apply_pending_preset(void)
{
    unsigned p = csd_vol_load(&csd_dsp_preset_req);
    if (p == CSD_DSP_PRESET_NONE || p == csd_dsp.preset) return;
    csd_dsp.preset = p;
    csd_dsp_restart();                   /* clears stale state: no pop */
}

/* Read by the AUDIO thread for the CSD_DSP_DIAG_BUMP stamp, so it must go
 * through the volatile accessor like every other cross-thread field. */
static unsigned csd_dsp_bump_read(void) { return csd_vol_load(&csd_dsp_bump); }

/* No-op, deliberately. It exists only because the board calls it from
 * csd_AudioHwConfig on every stream (re)configuration.
 *
 * It MUST NOT post the boot default here, and doing so was the real reason
 * the audio thread never honoured a host preset. AudioHwConfig is re-invoked
 * every few hundred ms while a stream is live (the same reason set_rate must
 * be idempotent -- see its comment), so re-posting CSD_DSP_PRESET_DEFAULT on
 * each call re-asserted the boot preset over the host's request within a
 * few hundred ms of every command. Measured: the host posted 0, the audio
 * thread's own stamp reported 2 for the entire capture, while the diagnostic
 * thread's readback reported 0 -- and the diagnostic thread's bump counter
 * advanced normally, proving the two threads share memory, so this was a
 * logic race rather than a memory barrier.
 *
 * The boot default is therefore the initialiser of csd_dsp_preset_req above:
 * it is adopted once, at the first stream start, and a host command is never
 * overwritten by a later reconfiguration. */
void csd_dsp_preset_boot(void) { }

/* Readback for DIAG:DSP:PRESet? -- runs on the DIAGNOSTIC thread, so it must
 * not read csd_dsp.preset (audio-thread-owned; its load could be hoisted in
 * this thread exactly as the audio thread's was in the other direction). The
 * posted request is the truthful answer to "what did the host ask for"; the
 * audio thread's applied value is verified separately through the mic-path
 * stamp, which is the only channel that provably crosses to the host. */
unsigned csd_dsp_get_preset(void)
{
    unsigned p = csd_vol_load(&csd_dsp_preset_req);
    return p == CSD_DSP_PRESET_NONE ? CSD_DSP_PRESET_BOOT : p;
}


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
    /* Adopt any preset the diagnostic thread posted, here at a frame boundary
     * where no sample is mid-computation. Only this thread writes csd_dsp.
     * See csd_dsp_set_preset for why the diagnostic thread may not. */
    csd_dsp_apply_pending_preset();

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
#ifdef CSD_DSP_DIAG_BUMP
    /* Thread-ownership discriminator, on the mic path L channel.
     *
     * csd_dsp_bump is incremented by the DIAGNOSTIC thread inside
     * csd_dsp_set_preset(); this line is executed by the AUDIO thread. So what
     * the host reads in L answers the one question that decides the fix:
     *   bump advances  -> the threads share memory; the frozen preset was a
     *                     hoisted load, and volatile accessors are the fix.
     *   bump frozen    -> the threads see different memories (the task landed
     *                     on another tile, which has its own RAM); no shared
     *                     global will ever bridge them and the preset must
     *                     travel over a channel instead.
     *
     * Counter sits in bits 8..23, not 0..15: the capture path is 24-bit and
     * the low byte is what gets lost (the hook-magic comment above records
     * exactly that), and the low byte of a counter is precisely the bits that
     * change -- losing them would make a live counter look dead. */
    sampsFromAudioToUsb[0] = (csd_dsp_bump_read() << 8) & 0x00FFFFFFu;
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
#if defined(CSD_GEN_EN) && !defined(CSD_GEN_PREMIX)
    /* DSD/DoP suppression -- user requirement: if the stream presents as
     * native DSD or DoP, the generator must not touch the samples.
     *
     * UserBufferManagement runs in ALL stream modes: the `if (!dsdMode)`
     * guard in lib_xua's deliver loop closes at xua_audiohub.xc:516 and the
     * DoSampleTransfer + UBM call sit OUTSIDE it, so an armed generator would
     * overwrite DSD payload words on the way to p_dsd_dac. (Native DSD needs
     * all 32 bits untouched -- mixer.xc's own volume path carries the
     * matching "we need all 32bits for Native DSD" note for exactly this
     * hazard.)
     *
     * Ordering matters twice:
     *  - DoP voting runs on the RAW host frame, BEFORE injection overwrites
     *    it. Vote after injection and the generator's own samples mask the
     *    DoP markers -- lib_xua's DoDsdDopCheck (which runs after us, on the
     *    same buffer) would never latch and the stream would never enter DoP
     *    at all while the generator was armed.
     *  - the injection gate reads BOTH flags: csd_dsp.dsd (set by
     *    csd_dsp_set_dsd from the live dsdMode at every stream start --
     *    covers native and post-latch DoP) and csd_dsp.dop (our in-band
     *    latch -- covers the DoP-presented-as-PCM frames before lib_xua
     *    notices). Either one silences the generator for that frame.
     *
     * The generator stays ARMED through a DSD stream (SCPI state still shows
     * the wave) but emits nothing; when the host returns to plain PCM, the
     * leave vote (eight non-matching raw frames) clears dop and the wave
     * resumes -- no re-arm needed. */
    /* Adoption always runs at the frame boundary, even while DSD suppresses
     * emission: a GENerator:OFF posted during a DSD stream must be adopted
     * NOW, not resurrected as a live wave eight frames after the host
     * returns to PCM. Gating only the emission keeps the cmd/adopted
     * handshake honest while the stream is not the generator's. */
    csd_gen_apply_pending();
    /* armed counted per frame: the rig measures emit vs suppressed by the
     * DELTA of the firmware's own counters across a stream segment -- the
     * mic capture cannot judge this at DoP rates (48 kHz-fixed codec;
     * arecord resamples silently and the spectrum was fiction). */
    unsigned armed = (csd_gen_state_mode() != (unsigned)CSD_GEN_MODE_OFF);
    if (armed && !csd_dsp.dsd)
        dop_check(sampsFromUsbToAudio);       /* vote on the RAW frame */
    if (!csd_dsp.dsd && !csd_dsp.dop) {
        csd_gen_inject(sampsFromUsbToAudio);
        if (armed) csd_gen_note(0u);          /* wave owned the frame */
    } else if (armed) {
        csd_gen_note(csd_dsp.dsd ? 1u : 2u);  /* suppressed: DSD vs DoP */
    }
#endif
#ifdef CSD_GEN_PREMIX
    /* Mixer build: NOTHING happens here. The wave was summed upstream, on the
     * mixer thread, before doMix() -- which is the whole point (the tone rides
     * the mixer's gain, routing and mapping like real audio). This thread must
     * not call apply_pending/inject/fill in that mode: two threads advancing
     * one NCO outputs exactly twice the commanded frequency, and both calling
     * apply_pending races the cmd/adopted handshake. The counters still
     * advance, from the mixer thread, so the rig reads the same
     * emit/sup_dsd/sup_dop triple either way. */
#endif
#if defined(CSD_GEN_EN) && defined(CSD_GEN_INSTRUMENT) && defined(MIXER) && (MIXER)
    /* Node 5: the words as the audio() task RECEIVED them, before the FIR.
     * Paired with node 6 after csd_dsp_frame(). These two are the only
     * instrumented view of the I2S-bound words in a MIXER build: the mixer
     * nodes (0..4) stop upstream of this thread, and the USB capture mirror
     * (DIAG_LOOP) is dead here because mixer1's GiveSamplesToHost owns the
     * capture path. rig/first_stream_regdiff.py found ES9219Q reg 0x43
     * (DPLL_NUM) moving to 0xFE exactly while the case-A tone is audible and
     * 0x00 when clean -- the chip is correcting an actively moving input, so
     * whether REAL content reaches I2S has to be measured at the source. */
    csd_mb_note_dac_sample(0u, CSD_NODE_PREDSP, (int)sampsFromUsbToAudio[0]);
    csd_mb_note_dac_sample(1u, CSD_NODE_PREDSP, (int)sampsFromUsbToAudio[1]);
#endif
    csd_dsp_frame(sampsFromUsbToAudio);
#ifdef CSD_GEN_EN
    csd_mb_note_dac_sample(0u, CSD_NODE_POSTDSP, (int)sampsFromUsbToAudio[0]);
    csd_mb_note_dac_sample(1u, CSD_NODE_POSTDSP, (int)sampsFromUsbToAudio[1]);
#endif
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

#else /* !PCM_DSP_EN ------------------------------------------------------ */

/* The diagnostic surface must exist in EVERY build, including bisect builds
 * that compile the engine out: csd_diag_scpi.c references these four
 * unconditionally, and a link error there costs a full rebuild cycle just to
 * ask a question. Semantics are honest, not fake: with no engine there is no
 * preset (0 = OFF, the boot value anyway), no stream mode seen, no DoP latch.
 * Declared in csd_dsp.h; the guarded half above owns the real state. */
unsigned csd_dsp_diag_dsd_flag(void)    { return 0u; }
unsigned csd_dsp_diag_dop_latched(void) { return 0u; }
unsigned csd_dsp_get_preset(void)       { return 0u; }
void csd_dsp_set_preset(unsigned p)     { (void) p; }

#endif // PCM_DSP_EN
