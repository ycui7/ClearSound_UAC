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
#include "csd_ups_block.h"     /* resampler client of the same block engine */
#ifdef CSD_GEN_EN
/* Interface only. csd_gen_core.h -- which carries the ~20 KB generated tables
 * as file-scope statics -- is included by exactly one translation unit,
 * csd_gen.xc; including it here as well would emit a second copy into a
 * tile's 512 KB of reserved memory. See the header comment in csd_gen.h. */
#include "csd_gen.h"
#include "csd_power_state.h"
#endif
#include "csd_dsp_coeffs.h"
#include "csd_dsp_core.h"
#include <string.h>
#include <stdint.h>
#include <xs1.h>   /* 2026-11-10: timerafter builtin for the EQ task's idle
                    * select (all in-tree timerafter users include this too) */
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

/* Witnesses for the 384 kHz left-channel EQ fault (2026-11-10). Same rule as
 * g_csd_bq_* in csd_dsp_bq.c: globals unconditional (the SCPI file does not
 * receive CSD_EXTRA_FLAGS), increments behind CSD_EQ_DIAG_WITNESS, writer is
 * the audiohub core only. ubm_calls counts UserBufferManagement entries (the
 * host-pair delivery cadence); eq_frames/eq_delay_frames count the branches
 * taken inside csd_dsp_frame; eq_ticks_max is the worst single burst, in
 * reference (10 ns) ticks, around the two adsp_biquad VPU calls. REVERT:
 * delete these, the guarded blocks, and the INFO? tail fields they feed. */
unsigned g_csd_ubm_calls;

/* Armed-mic feed mailbox (2026-11-11, F-B). DEFINED HERE on purpose: these
 * sit with the audiohub hot loop's own data -- the UBM feed reads them every
 * wire frame and 11.11.7 measured that even one cross-file call per frame
 * (csd_ups_mic_owner) blows the 384 kHz budget. Producers (csd_ups_block.c,
 * SAME core as this loop): the cold push helper writes the gate, the tap
 * commit in csd_ups_slot publishes the pair. Same-core sequential access;
 * plain (not volatile) XC globals like g_csd_ubm_calls -- each side touches
 * them once per function call, so every access is a fresh load/store at a
 * call boundary.
 * REVERT: delete these and the UBM feed block + the .c/.h producers. */
unsigned csd_feed_owner = 0u;
int      csd_feed_w0    = 0;
int      csd_feed_w1    = 0;
/* EXPERIMENT 11.11.34 dual echo (REVERT with the tap): the pop-word pair
 * committed by the tap (csd_ups_block.c) — the second stream the mic
 * carries. The mirror time-multiplexes: even mic frames = feed-IN pair
 * (engine input), odd mic frames = latest committed pop pair (engine
 * output). One stream, both seams, same host cadence — the comparator
 * de-interleaves. csd_mirror_parity: audio core only (this loop). */
int      csd_pop_w0     = 0;
int      csd_pop_w1     = 0;
unsigned csd_pop_seq    = 0u;   /* 11.11.40 freshness: bumped per commit */
/* 11.11.36: parity multiplexing assumed audio()-call == mic-frame rate —
 * true at ratio 1, unknowable at ratio > 1. The mirror now publishes ONE
 * pair per mic frame, selected at runtime by DIAG:UPSAMPLing:ECHO:
 * 0 = pop pair (engine output), 1 = feed pair (engine input, published
 * from audio() entry this build). Two passes of the same deterministic
 * schedule: pass 1 proves the engine input IS the schedule, pass 2
 * scores the pop stream against the oracle fed by the schedule. */
unsigned g_csd_ups_echo_mode = 0u;
unsigned g_csd_ups_mirror_seq = 0xFFFFFFFFu;  /* last pop seq the mirror saw */
/* REVERT(bp21): ECHO-0 mirror stride-jitter witnesses. The mirror used to
 * gate on a pop seqlock and re-emit the previous pair whenever the seq had
 * not moved, which made the emitted wire stride alternate (d = j - R*i in
 * 15/14-frame runs, net zero) and cost ~6% coverage at 192k. The pop side
 * (csd_ups_block.c, single writer, same core) now publishes ONE pair per
 * host frame at a fixed slot phase, and the mirror copies it every frame
 * (zero-order hold when no new pair landed). One decision, one place: the
 * selection lives with the pop counter, not with the reader's timing.
 *   g_csd_mir_pub_n  -- pairs published (pop side)   */
unsigned g_csd_mir_pub_n  = 0u;
unsigned g_csd_mir_copy_n = 0u;   /* mirror copies (one per audio frame)     */
unsigned g_csd_mir_hold_n = 0u;   /* copies with no new pair since the last  */
/* REVERT(bp31): mic-frame-aligned mirror state (see the ECHO-0 branch of
 * UserBufferManagement). s_mir_cur indexes the pop side's phase-0 pair series
 * in PAIR units (one pair = one host frame = R wire words) and advances by
 * exactly 1 per host frame, so the emitted stream is a FIXED decimation of
 * the pop stream: the capture's d = j - R*i is constant for the whole
 * window instead of dithering by +-1 word in 3..19-frame runs. s_mir_w0/w1 is
 * the zero-order hold register (honest repeat when the pop side has not yet
 * published the pair this frame needs). */
static unsigned s_mir_cur = 0u;
static int      s_mir_w0 = 0, s_mir_w1 = 0;
unsigned g_csd_mir_relock_n = 0u;  /* FIFO window escapes (re-locks)          */
unsigned g_csd_mir_lag_n = 0u;     /* live skew prod_n - cursor at last read  */
unsigned g_csd_feed_writes = 0u;   /* INFO? field 98 witness: feed stores done
                                    * by UBM (REVERT with the feed) */
unsigned g_csd_eq_frames;
unsigned g_csd_task_loops, g_csd_task_past_take;   /* REVERT(dp172) */
unsigned g_csd_eq_delay_frames;
unsigned g_csd_eq_ticks_max;
/* Sub-region UBM cost witnesses (2026-11-10): the audiohub loop at 384 kHz
 * has ZERO compute margin (preset-0 control runs the loop at exactly the
 * wire rate), so every UBM tick competes with the LRCLK deadline. Totals and
 * maxima in reference (10 ns) ticks per region: 0 apply_pending, 1 pw_note
 * pair, 2 gen block, 3 csd_dsp_frame, 4 whole UBM. REVERT with the block
 * above. */
unsigned g_csd_ubm_reg_tot[5];
unsigned g_csd_ubm_reg_max[5];

/* ===================== per-frame EQ relocation (2026-11-10) =================
 * The audiohub loop generates LRCLK IN SOFTWARE: its compute time IS the wire
 * rate (preset-0 control measured 383,736 frames/s at 384 kHz, i.e. the loop
 * body costs ~260.6 reference ticks against the 260.4-tick frame period --
 * zero margin). The mixer1 task is NOT a second schedule: with MAX_MIX_COUNT=0
 * it is a synchronous relay INSIDE the per-frame exchange -- audiohub sends
 * the token (StartSampleTransfer), then blocks in CompleteSampleTransferUsbChans
 * on inuint(c_aud) until mixer1's GiveSamplesToDevice returns, so compute
 * placed there serialises with the LRCLK deadline exactly as inline does
 * (mixer.xc's own instrument documents it: "audio() BLOCKS between sending
 * this token and getting the sample exchange back, and its I2S port deadline
 * is the hard one"; ClearSoundDac.xn: the relay is "the dominant term"
 * inside audio()'s half-frame deadline -- MIXER=0 preset 0 reached 0.9995
 * capture fill where MIXER=1 sat at 0.764). It must therefore run on its own
 * task/core, AN02011/AN02014 style: the bit-timed side publishes one word per
 * channel per frame into a lock-free single-producer/single-consumer ring and
 * takes back the filtered word; all biquad compute happens in csd_dsp_eq_task
 * on its own core (tile[1], spawned from USER_MAIN_CORES). The per-slot pop
 * never blocks and never computes: bounded stores/loads plus counter
 * increments, nothing else.
 *
 * Ring discipline (no drop, no hold, no repeat -- owner rule):
 *   counters are absolute frame numbers (wrap-safe subtraction);
 *   a pushed frame is consumed exactly once by the task and emerges exactly
 *   once on the out ring, in order;
 *   if the in ring is FULL the task cannot keep up: push and pop are skipped
 *   as a pair (the frame passes through unfiltered, sequence stays aligned),
 *   g_csd_eq_overrun asserts -- this is a qualification failure, it must read
 *   0 in every passing run;
 *   if the out ring is EMPTY (pipeline fill on enable, or a dead task) the
 *   frame likewise passes through unfiltered and g_csd_eq_underrun asserts.
 * Latency: pipeline depth ~1 frame once the task is hot (the task drains
 * immediately), bounded above by the ring (64 frames = 167 us at 384 kHz).
 * The shipped default kernel IS the identity (csd_dsp_bq.c, 2026-11-10), so
 * the preset-2 bit-perfect gate is unaffected by the fill frames because the
 * default kernel is the identity; host-composed kernels are adopted through
 * the same config/resync/engage path via DIAG:DSP:COEFFicient.
 *
 * Cross-thread visibility: same-tile DP-RAM globals driven through the
 * csd_vol_load/csd_vol_store volatile-pointer accessors already proven in this
 * file (xcc's single-writer model may otherwise hoist a plain load across the
 * blocking calls of the streaming loop; the csd_vol_mailbox seqlock in the
 * same app is the second precedent). Ring DATA words are accessed through
 * volatile int casts for the same reason; each word is one aligned 32-bit
 * access, so torn reads are impossible by width.
 *
 * The engine's own state (bq_state/bq_c/bq_ready) becomes task-owned: config
 * and clear are REQUESTS posted by the audio core (csd_eq_cfg_post) and
 * ADOPTED by the task before it touches the engine -- the same post/adopt
 * doctrine as csd_dsp_preset_req. CSD_DSP_INLINE_EQ keeps the old inline
 * branch compiled-out-but-present as the A/B control (the collapsed-rate arm
 * of this investigation); disabling it is NOT deleting it.
 *
 * Storage and entry points live in csd_eq_ring.{h,c}: XC's whole-program
 * parallel-usage analysis refuses plain globals touched from two par
 * branches (measured: "use of 'usage.anon.N' violates parallel usage rules"
 * at main.xc:537), while C statics behind scalar-only function calls are
 * opaque to it -- the csd_vol_mailbox.c precedent. The per-frame path below
 * is therefore two C calls (csd_eq_is_engaged + csd_eq_tx) plus two staged
 * word reads: bounded stores/loads, no compute, no blocking. */
#include "csd_eq_ring.h"

extern unsigned s_feedcalls_task;   /* dp108 feed-call attribution */
unsigned g_csd_ep0, g_csd_ep1, g_csd_ewz;      /* REVERT(bench) */
unsigned g_csd_fb1, g_csd_fb2;                    /* REVERT(bench) */
extern unsigned s_feedcalls_audio;
extern unsigned g_csd_probe_mask;   /* REVERT(dp199): attribution mask for
                                     * hot-path diagnostic gates (defined in
                                     * xua_audiohub.xc; same cross-TU read
                                     * idiom as g_csd_ups_echo_mode's
                                     * setters accept from the SCPI task). */

static void csd_ubm_note(unsigned idx, unsigned a, unsigned b)
{
    unsigned d = b - a;
    g_csd_ubm_reg_tot[idx] += d;
    if (d > g_csd_ubm_reg_max[idx]) g_csd_ubm_reg_max[idx] = d;
}

static void csd_dsp_restart(void)
{
    csd_dsp.head = 0;
    memset(csd_dsp.ring, 0, sizeof csd_dsp.ring);
    /* Re-arm the FIR for the active preset; never leave the FIR preset
     * silently unfiltered (no degraded-mode bypass). Post-adopt: the engine
     * now belongs to the EQ task core, so the request goes through the
     * cfg seq and the task executes config/clear itself (see the relocation
     * block above). */
    csd_eq_cfg_post(csd_dsp.preset >= CSD_DSP_PRESET_FIR && csd_dsp.rateIdx >= 0,
                    csd_dsp.rateIdx >= 0 ? (unsigned)csd_dsp.rateIdx : 0u);
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
        /* REVERT(dp199): divided by 32 (same reason as the UBM preset
         * adopt: the xt_ubm witnesses price the csd_gen_mix_dop cross-TU
         * read + compare at the top of the 59 t dsp region; the verdict is
         * a level from the mixer voter and this branch only adopts an
         * EDGE. This product has no DSD path at all (dsdMode is forced
         * OFF at receive_command), so the DoP bypass latch carries audio
         * only through the voting in csd_gen_mix_begin — the adopt
         * latency this divider adds is <=167 us. */
        static unsigned csd_dop_div = 0u;
        if ((++csd_dop_div & 31u) == 0u) {
        unsigned d = csd_gen_mix_dop();
        if (d != csd_dsp.dop) {
            csd_dsp.dop = d;
            csd_dsp_restart();
        }
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

    /* ONE block engine, TWO INDEPENDENT CLIENTS (owner directive 2026-11-10,
     * correcting the merged "the upsampler IS the DSP / shelf stands down"
     * doctrine -- that made the DSP subordinate to the sample-rate converter
     * and killed effects at 384 kHz, where the resampler has no family entry
     * at all). The DSP below runs on EVERY host rate, gated only by its own
     * preset and the stream format (DSD/DoP bypass). The resampler is a
     * second client: it takes the pair AFTER the DSP has filtered it, at
     * host-pair cadence; when it is active its wire words replace the
     * host-rate words at the I2S pop (csd_ups_slot). csd_ups_feed is a
     * one-load no-op while the resampler client is off, so an unarmed stream
     * pays nothing for the second client existing. DoP/DSD voting ran
     * already, above, on the host-rate frame. */

    unsigned n = csd_dsp.n;
    unsigned eq_pushed = 0u;   /* 1 = frame handed to the EQ task (it feeds) */
    if (n != 0u && !csd_dsp.dsd && !csd_dsp.dop && csd_dsp.preset != 0u) {
        unsigned h = csd_dsp.head;
        if (csd_dsp.preset == 1u && n != 0u) {
            /* Defense-in-depth (2026-11-22): DELAY arithmetic on n==0
             * underflows (D=(0-1)/2) and reads the ring out of bounds. The
             * outer gate already requires n!=0; this pins the invariant at
             * the use site too — a future caller that loosens the outer
             * gate cannot silently introduce an OOB read here. */
            /* DELAY preset: y[t] = x[t-D], D=(n-1)/2 (same delay the FIR applies).
             * Ring size n: the value at index (t-D) mod n was written at t-D and
             * has not been overwritten since. */
            const unsigned D = (n - 1u) / 2u;
            unsigned r = h + n - D;
            if (r >= n) r -= n;
#ifdef CSD_EQ_DIAG_WITNESS
            /* REVERT(dp204): bit5-gated (was unconditional): the DELAY
             * preset path runs per frame on the wire core at PROBE legs
             * (passT uses a preset), so this RMW was 384k/s on the
             * zero-margin ratio-1 loop. */
            if (g_csd_probe_mask & 32u) g_csd_eq_delay_frames++;
#endif
            for (unsigned ch = 0; ch < 2; ch++) {
                int32_t y = csd_dsp.ring[ch][r];      /* 'out' is an XC keyword */
                csd_dsp.ring[ch][h] = (int32_t)s[ch];
                s[ch] = (unsigned)y;
            }
            csd_dsp.head = next_head(h, n);
        }
#ifdef CSD_DSP_INLINE_EQ
        /* A/B control of the relocation measurement (see block above): the
         * inline engine cost ~121-154 ticks per frame on the bit-timed loop
         * and collapsed the software LRCLK from 383,736/s to 255,830/s at
         * 384 kHz. Disabled by default (owner rule: disabled config stays
         * with a why-note); define to reproduce the collapsed arm. */
        else if (CSD_ENGINE_READY()) {
            timer eqt; unsigned eq0, eq1;
            eqt :> eq0;
            CSD_ENGINE_FRAME(s);
            eqt :> eq1;
            g_csd_eq_frames++;
            { unsigned d = eq1 - eq0; if (d > g_csd_eq_ticks_max) g_csd_eq_ticks_max = d; }
        }
#endif
        else {
            /* Relocated EQ (see the relocation block above): ONE C call per
             * frame does the whole O(1) hand-off -- engaged check, ring
             * fullness, publish one word per channel, stage the filtered
             * pair (or the input pair, honestly counted, when the pipeline
             * is filling). No blocking, no compute. The return + staged
             * words ARE this frame's wire pair on every path, so the
             * assignment below is unconditional and bypass stays byte-exact
             * by construction (identity values come back). */
            /* EXPERIMENT 11.11.36 feed-IN echo MOVED HERE from
             * csd_ups_feed (11.11.29): the task publishes at its own
             * bursty pace, so the mic view of the engine input lost or
             * duplicated values arbitrarily. The UBM pair at THIS point
             * IS what the engine will receive on this mic frame, on the
             * mic's own clock: one value per mic frame, duplicates only
             * when audohost genuinely repeated one. Preset 2's biquad is
             * identity (campaign-verified), so pre == post at this seam.
             * REVERT with the tap. */
            /* 2026-11-12 ROOT CAUSE of the chronic pop-vs-oracle 0%:
             * the engine is fed the POST-handoff (kernel-filtered) pair —
             * the EQ task feeds what csd_eq_handoff stages. The old tap
             * published PRE-handoff values, so the oracle was fed the
             * unfiltered pair while the device engine saw the filtered
             * one: no alignment could ever exist. The honest engine
             * input is the STAGED pair — publish it here, same mic-frame
             * cadence (audio core, one publish per frame, duplicates only
             * when audohost repeated). Preset-1/bypass paths feed raw via
             * the tail call below and never reach this branch, so the tap
             * now follows the feed in every path.
             * REVERT: restore publish above the handoff. */
            s[0] = csd_eq_handoff(s[0], s[1]);
            s[1] = csd_eq_stage_r;
            eq_pushed = csd_eq_pushed;
            /* REVERT(dp199): feed-mirror gated on echo!=2 — the ONLY
             * readers of csd_feed_w0/w1 are the ECHO-1 mic-mirror branch
             * below (and the FEED? SCPI view, meaningless under ECHO 2
             * because the mirror that would carry them is skipped). The
             * two cross-file global stores sat on every transfer frame of
             * the wire thread for nothing. */
            if (csd_feed_owner && g_csd_ups_echo_mode != 2u) {
                csd_feed_w0 = (int)s[0];
                csd_feed_w1 = (int)s[1];
            }
        }
    }                                            /* else: byte-exact bypass */
#ifdef CSD_UPSAMPLE_EN
    /* Host-pair feed to the resampler client. Frames handed to the EQ task
     * are fed there, AFTER filtering (same DSP->resampler order as before);
     * every other frame (bypass, delay preset, fill/overrun pass-throughs)
     * is fed here, exactly as before. */
    /* 2026-11-12 SINGLE-WRITER ENFORCEMENT (measured: owner-armed cert run
     * with eq_overrun=162k showed feed_enter=162k — every failed task push
     * fed the RAW pre-bq pair from the audio core while the task was also
     * feeding post-bq pairs: dual-writer, wrong content, 0.5% extra feeds
     * shifting the fd block phase (eq_underrun 1271 ≈ the same events).
     * That corrupts EVERY ratio>1 measurement ever taken through this
     * path. When the task owns the feed, a failed push DROPS the frame;
     * the engine honestly starves instead of being poisoned. The armed
     * test closes the adoption race: during PAUSE/CONFIG the owner mirror
     * reads 0 while the task is mid-config on the SAME fd state — a
     * pass-through there is a dual-writer and wedged fd_block_fir_compute
     * (11.12.6 witness: feed_pc=3, task frozen, eq_frames static). Armed
     * means task-owned, full stop (csd_eq_ring.c enqueues even when
     * disengaged while armed, for exactly this reason). */
    /* REVERT(bench 2026-11-17): DEBUG:CSD:PROBE bit5 (32) = EQ-BYPASS FEED
     * mode for upsampler qualification. With PRESet 0 the EQ task never
     * takes (queue empty) so this is still a SINGLE feed writer — the fd FIR
     * state has one owner (this core) exactly as the disarmed case. */
    g_csd_fb1++;   /* REVERT(bench): frames reaching the fallback gate */
    /* SHIPPING FEED for the engine-off armed mode (PRESet 0 = resampler
     * only, no EQ kernel): 2026-11-22. While the task has not adopted an
     * engine image, engaged==0 means NOTHING can take the ring (handoff is
     * never called at PRESet 0: the outer gate above), so the task can
     * never be a feed writer here — the audio core is the ONE owner of the
     * fd FIR state, and crucially it feeds on the SAME thread that pops:
     * feed/pair cadence is self-consistent with the wire loop. The task
     * path cannot provide that (measured r8 legs: enqueue+take+feed all
     * alive at 79.3k/s through the task, FD still starved — cross-core
     * feed pacing vs pops never phase-aligns; same-thread bit5 feed is
     * audible). Single-writer invariant preserved: the moment the task
     * engages (PRESet!=0 paths), engine_off drops and the tail closes. */
    const unsigned engine_off = !csd_eq_dbg_eng();
    if (!eq_pushed &&
        (!csd_feed_owner || (g_csd_probe_mask & 32u) || engine_off) &&
        (!csd_ups_armed() || (g_csd_probe_mask & 32u) || engine_off)) {
        g_csd_fb2++;   /* REVERT(bench): fallback feed taken */
        s_feedcalls_audio++;
        csd_ups_feed((int)s[0], (int)s[1]);
    }
    /* REVERT(bench): audio-path word content + push attribution */
    if (eq_pushed) g_csd_ep1++; else g_csd_ep0++;
    if (s[0] | s[1]) g_csd_ewz++;
#endif
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

    /* A REAL rate change drops any tester-installed kernel: overrides were
     * composed against the old rate and are meaningless at the new one, and
     * the board must never sit in a shipping configuration wearing them
     * (volatility contract, csd_dsp_bq.c). The first stream after boot is
     * NOT a rate change (rateIdx < 0) -- that is the pinned-before-open
     * pattern the bit-perfect gate arms with, and the config-time rate stamp
     * in csd_dsp_bq_config is what protects a stale post there. */
    if (csd_dsp.rateIdx >= 0) csd_dsp_bq_coef_restore();

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
        /* Engine config is adopted by the EQ task core (post/adopt, see the
         * relocation block); the audio core must not touch engine state. */
        if (csd_dsp.preset >= CSD_DSP_PRESET_FIR)
            csd_eq_cfg_post(1u, (unsigned)idx);
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
/* XC requires a declaration before first use; the definition is below with
 * the task (2026-11-10: the suspend-generation guard consumes it). */
static unsigned csd_dsp_bump_read(void);

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
#ifdef CSD_IDLE_POWER
    /* Idle-power DSP bypass (TODO item 7), adopted HERE because the engine
     * belongs to this thread. csd_gen_mailbox.c documents why the server may
     * not post presets to us directly.
     *
     * LAST-APPLIED tracking, not edge detection: the request is a level, so
     * if both transitions land between two frames the level we last acted on
     * still differs from the level requested and we correct it. A pure
     * edge-triggered read could strand the filter off with power nominally
     * on -- audible, silent, and invisible to every query. */
    {
        static unsigned pw_applied = 0;             /* 0 = running normally */
        /* Suspend-generation guard (2026-11-10, mid-stream config audit):
         * csd_pw_take_saved_preset() used to overwrite the host's request
         * whenever the request word read 0 at wake -- and a host "PRESet 0"
         * posts 0 too, so a preset written while the box was powered down
         * was silently replaced by the pre-suspend preset at wake (measured:
         * PRESet 0 written cold, PRESet? answered 2 for the whole run).
         * The saved preset is only the host's intent if the host has said
         * nothing SINCE the suspend: csd_dsp_bump counts every set_preset
         * command, so restore only while the bump is frozen at the value
         * observed at save. A host command during suspend wins outright. */
        static unsigned pw_saved_bump = 0;
        unsigned pw_req = csd_pw_dsp_bypass_requested();
        if (pw_req != pw_applied) {
            if (pw_req) {
                csd_pw_save_preset(csd_dsp.preset); /* save live preset once */
                pw_saved_bump = csd_dsp_bump_read();
                csd_vol_store(&csd_dsp_preset_req, 0u);
            } else {
                unsigned sv = csd_pw_take_saved_preset();
                /* Do NOT clobber a preset the host posted while we were
                 * bypassed: any host command moves the bump, and if the
                 * request word moved off 0 after we wrote it, that is a
                 * real host command and it wins. (The diagnostic thread
                 * only ever posts through csd_dsp_set_preset, so anything
                 * non-zero here is the host's intent, never our own bypass
                 * residue.) */
                if (sv != CSD_PW_PRESET_NONE &&
                    csd_dsp_bump_read() == pw_saved_bump &&
                    csd_vol_load(&csd_dsp_preset_req) == 0u) {
                    csd_vol_store(&csd_dsp_preset_req, sv);
                }
            }
            pw_applied = pw_req;
        }
    }
#endif
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
#ifdef CSD_EQ_DIAG_WITNESS
    /* REVERT(dp204): bit5-gated (was unconditional under the build flag).
     * UBM runs on the audiohub core once per frame: at ratio 1 this RMW
     * ran 384k/s on the zero-margin wire loop. Reads boot value at
     * PROBE 0; set DEBUG:CSD:PROBE 32 BEFORE the stream to arm. */
    if (g_csd_probe_mask & 32u) g_csd_ubm_calls++;
#endif
#if defined(CSD_EQ_DIAG_WITNESS) && defined(CSD_UBM_TIMING)
    /* REVERT(dp160 2026-11-13): the five region timers (6 reads + 5 notes
     * per UBM call = ~50 ticks) are pure instrumentation on the armed
     * wire-critical path — the exact hazard this file's CSD_MB_I2S_MONITOR
     * note documents ('instrumentation that changes the timing of the path
     * it measures is worthless'). Measured armed loop 306k/s with them;
     * the g_csd_ubm_calls counter stays under CSD_EQ_DIAG_WITNESS. The
     * region witnesses compile only with -DCSD_UBM_TIMING (reads zero
     * otherwise, same contract as the xfer fields). */
    timer ubt; unsigned u0, u1, u2, u3, u4, u5;
    ubt :> u0;
#endif
    /* Adopt any preset the diagnostic thread posted, here at a frame boundary
     * where no sample is mid-computation. Only this thread writes csd_dsp.
     * See csd_dsp_set_preset for why the diagnostic thread may not.
     * REVERT(dp199): divided by 32 (every 32nd host frame ~= 167 us at
     * 192 kHz, 83 us at 384 kHz). WHY: the xt_ubm attribution witnesses
     * (armed 192k leg) price this call at 14.4 t/transfer on the wire
     * thread's transfer frame — it is a level-adopting control-plane read
     * (bypass level + posted preset), not a per-frame data path, and the
     * only latency commitments are control-plane (a PRESet command is
     * echoed from the request word immediately; stream (re-)config adopts
     * undivided in csd_dsp_set_rate; the idle-power bypass adopts
     * with a 167 us worst case, orders under the power-state dwell). */
    { static unsigned csd_ap_div = 0u;
      if ((++csd_ap_div & 31u) == 0u) { csd_dsp_apply_pending_preset(); } }
#if defined(CSD_EQ_DIAG_WITNESS) && defined(CSD_UBM_TIMING)
    ubt :> u1; csd_ubm_note(0, u0, u1);
#endif

#ifdef CSD_IDLE_POWER
    /* Idle-power stream witness (TODO item 7). MUST stay at the entry, ahead
     * of every diagnostic guard. First attempt sat inside
     * `#if defined(CSD_GEN_EN) && defined(CSD_GEN_INSTRUMENT) && MIXER`,
     * which SPECIALTYCIRCUITS_RELEASE does not define: the release image
     * powered DOWN fine (that leg is driven by the ep0 alt-0 trigger, a
     * different path) but could never wake, because the wake condition is
     * this counter and it had been compiled out -- measured on hardware
     * 2026-11-05: tile0 and tile1 both read frames=0 through a live tone,
     * state stuck at OFF. Witness code is functional state, not
     * instrumentation, so it belongs outside the instrumentation region.
     *
     * A NON-ZERO DAC-bound frame is the only honest evidence of a stream:
     * LRCLK toggles with a silent bus. Cost: two compares on the pair, once
     * per frame, at the point where the host stream is actually pulled. */
#if defined(CSD_EQ_DIAG_WITNESS) && defined(CSD_UBM_TIMING)
    ubt :> u2;
#endif
    csd_pw_note_host_pair((int)sampsFromUsbToAudio[0],
                          (int)sampsFromUsbToAudio[1]);  /* REVERT(dp199) */
#if defined(CSD_EQ_DIAG_WITNESS) && defined(CSD_UBM_TIMING)
    ubt :> u3; csd_ubm_note(1, u2, u3);
#endif
#endif

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
#if defined(CSD_EQ_DIAG_WITNESS) && defined(CSD_UBM_TIMING)
    ubt :> u2;
#endif
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
#if defined(CSD_EQ_DIAG_WITNESS) && defined(CSD_UBM_TIMING)
    ubt :> u3; csd_ubm_note(2, u2, u3);
#endif
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
    /* Single frame path: the DSP runs the pair per its own preset, then
     * hands it to the resampler client (see csd_dsp_frame -- one block
     * engine, two independent clients, neither gates the other). */
#if defined(CSD_EQ_DIAG_WITNESS) && defined(CSD_UBM_TIMING)
    ubt :> u4;
#endif
    csd_dsp_frame(sampsFromUsbToAudio);
#if defined(CSD_EQ_DIAG_WITNESS) && defined(CSD_UBM_TIMING)
    ubt :> u5; csd_ubm_note(3, u4, u5);
    csd_ubm_note(4, u0, u5);
#endif
#ifdef CSD_GEN_EN
    /* REVERT(dp199): POSTDSP peak notes gated behind probe bit5 — pure
     * diagnostic peak-hold on the transfer-frame path (xt_ubm: the two
     * notes + branch sit in the ~10 t residue between the four sub-region
     * timers and the calls). Read the DACPeak? window only in PROBE-32
     * legs. PREDSP notes are compiled out here anyway (GEN_INSTRUMENT). */
    if (g_csd_probe_mask & 32u) {
    csd_mb_note_dac_sample(0u, CSD_NODE_POSTDSP, (int)sampsFromUsbToAudio[0]);
    csd_mb_note_dac_sample(1u, CSD_NODE_POSTDSP, (int)sampsFromUsbToAudio[1]);
    }
#endif
#ifdef CSD_DSP_DIAG_RAW
    sampsFromAudioToUsb[1] = sampsFromUsbToAudio[0];
#endif
#ifdef CSD_DSP_DIAG_LOOP
    /* csd_dsp_frame rewrote the buffer IN PLACE, so it now holds exactly the
     * sample that goes to I2S/DAC. Mirror it onto the mic path: this measures
     * kernel output through the proven-good capture path, with no second pass
     * (a second call would double-advance the block cadence and corrupt the
     * very thing being measured). Pure stock mirror -- the armed resampler
     * feed lives OUTSIDE this ifdef (campaign builds do not define
     * CSD_DSP_DIAG_LOOP; the 11.11.3 run proved the armed mic read
     * uninitialized RAM because the old feed branch sat in here). */
    sampsFromAudioToUsb[0] = sampsFromUsbToAudio[0];
    sampsFromAudioToUsb[1] = sampsFromUsbToAudio[1];
#endif
#ifdef CSD_UPSAMPLE_EN
    /* Armed mic feed (2026-11-11, F-B; seam per glm53f reviews
     * deleg_d22b56ad). HOT-PATH RULE (measured, 11.11.7 bisect vs 11.11.8):
     * the disarmed hot path here is ONE load + branch on FILE-LOCAL data --
     * a cross-file call per UBM frame (csd_ups_mic_owner(), 11.11.7) blew
     * the 384 kHz audio() budget and broke the certified ratio-1 legs
     * (periodic ~192-word mic dropouts). The gate and the pair below are
     * written by the SAME core that runs this loop: csd_ups_slot's tap
     * commit (csd_ups_block.c, runs in this audio() iteration) publishes
     * the pair; cold config paths (set_want/set_enabled/note_wire_rate/
     * ups_reconfigure) publish the gate. When armed the words are the last
     * wire pair the pop-side tap committed; nothing else writes these mic
     * slots armed (ADC is not fed at wire rate) -- which is why the armed
     * mic read uninitialized RAM before these lines, while the same RAM
     * reaching the host proves the pipe. The 11.11.6 lib_xua mixer hook is
     * REVERTED and must stay reverted (cross-thread read of audiohub-owned
     * tap state + hot-path cost on the mixer task).
     * REVERT: delete this #ifdef block and the csd_feed_* symbols. */
    /* 2026-11-13 ECHO 2 (user goal: armed mic loopback must carry REAL ADC):
     * mode 2 skips the mirror overwrite so the words the audiohub port
     * decimator published from ADC1 reach the host untouched. REVERT with
     * the echo block. */
    if (csd_feed_owner && g_csd_ups_echo_mode < 2u)
    {
        if (g_csd_ups_echo_mode == 1u) {
            sampsFromAudioToUsb[0] = (unsigned)csd_feed_w0;
            sampsFromAudioToUsb[1] = (unsigned)csd_feed_w1;
        } else {
            /* REVERT(bp31): MIC-FRAME-ALIGNED deterministic emission. This
             * call IS the host mic-frame boundary (UBM runs once per USB
             * transfer = once per ratio wire frames; its sampsFromAudioToUsb
             * store is what the mic buffer ships), so the pair it writes is
             * the pair "destined for that host frame". The pop side publishes
             * one pair per host frame into the FIFO at ring phase 0; here we
             * take pair #s_mir_cur -- a monotone index that advances by
             * EXACTLY 1 per host frame -- so the emitted stream is a fixed
             * decimation of the pop stream and the capture maps with one
             * constant d instead of a +-1-word dither (bp21 fixed WHEN the
             * pair was written, not WHICH pair: the phase-0 pairs landed
             * 8+-1 per host frame, so latest-wins still dithered).
             * Reads are tear-free: same core, words stored before the count
             * stamp, and slot k is only reused CSD_MIR_Q pairs (~1.3 ms)
             * later. If the requested pair is not published yet this is an
             * honest zero-order hold (COUNTED); if the reader ever falls
             * CSD_MIR_Q/2 pairs behind, the slot would have been overwritten,
             * so it re-locks to the newest pair (COUNTED, one frame cost). */
            const unsigned pn = csd_mir_prod_n;
            if (pn > s_mir_cur) {
                if ((pn - s_mir_cur) > (CSD_MIR_Q / 2u)) {
                    s_mir_cur = pn - 2u;
                    g_csd_mir_relock_n++;
                }
                const unsigned qi = s_mir_cur & (CSD_MIR_Q - 1u);
                s_mir_w0 = (int)csd_mir_q0[qi];
                s_mir_w1 = (int)csd_mir_q1[qi];
                s_mir_cur++;
                g_csd_mir_copy_n++;
            } else {
                g_csd_mir_hold_n++;
            }
            g_csd_mir_lag_n = pn - s_mir_cur;   /* skew witness: bounded */
            sampsFromAudioToUsb[0] = (unsigned)s_mir_w0;
            sampsFromAudioToUsb[1] = (unsigned)s_mir_w1;
        }
        g_csd_feed_writes++;   /* INFO? field 98 (REVERT with the feed) */
    }
#endif
}

/* ---- the relocated EQ task (see the relocation block above) ----------------
 * Own core (spawned on tile[1] from USER_MAIN_CORES), own schedule: consume
 * host pairs from the in ring, run the biquad, publish to the out ring. The
 * only blocking construct is the idle backoff (a timer wait, so the scheduler
 * can run the rest of the tile); it is on the EMPTY side and never delays the
 * bit-timed loop, whose pop path is a pure memory transaction. Engine
 * config/clear is executed HERE, adopting posted requests -- engine state has
 * exactly one writer core after adoption. */
extern void thread_speed(void);
/* REVERT(dp201): task-side freeze witnesses (defined in csd_ups_block.c;
 * same volatile-in-C / plain-extern-in-XC pattern as csd_pop_seq). */
extern unsigned g_csd_eq_stage, g_csd_eq_stage_t;
extern unsigned csd_ups_now(void);

#ifndef CSD_DSP_INLINE_EQ
/* Not compiled in the inline A/B arm: there the frame hook writes the EQ
 * witnesses and a second writer would violate the parallel-usage rules. */
/* Kernel-update pause witnesses (2026-11-10, atomic block write): every
 * adoption runs PAUSE (engage 0: the bit-timed side falls to byte-exact
 * pass-through, no enqueue, no compute, LRCLK untouched) -> load/re-init ->
 * RESUME. Events counts adoptions; ticks_max is the worst engage(0)..engage()
 * window in reference (10 ns) ticks, task-core side. REVERT with the ring. */
unsigned g_csd_eq_pause_events, g_csd_eq_pause_ticks_max;

void csd_dsp_eq_task(void)
{
    thread_speed();
    unsigned applied_seq = 0xFFFFFFFFu, applied_eng = 0xFFFFFFFFu;
    unsigned applied_cseq = 0xFFFFFFFFu;   /* host-kernel aperture seq (bq.c) */
    timer t;
    timer pt;
    unsigned deadline = 0u;
    while (1) {
        unsigned seq = csd_eq_cfg_seq_get();
        unsigned eng = csd_eq_cfg_on_get();
        /* One volatile load per iteration: a kernel-container publication
         * from the diagnostic thread adopts HERE, through the same
         * pause/config/resync/engage block a PRESet change drives -- the
         * bit-timed loop is not involved (see the aperture block in
         * csd_dsp_bq.c). */
        unsigned cseq = csd_dsp_bq_coef_seq();
        if (seq != applied_seq || eng != applied_eng || cseq != applied_cseq) {
            applied_seq = seq; applied_eng = eng; applied_cseq = cseq;
            /* PAUSE / LOAD / RESUME (owner directive 2026-11-10): engaging
             * off FIRST makes the producer pass frames through unfiltered
             * (byte-exact, not counted as an overrun) so the ring drains and
             * no frame is EVER processed against a mixed or half-published
             * image; config re-inits engine state from a fully validated
             * copy; resync drops the stale backlog and bumps the epoch; the
             * resume engages the one complete new image. All of it runs on
             * THIS core -- the software-LRCLK loop pays nothing beyond the
             * engaged load it already does every frame. */
            csd_eq_engage(0u);
#ifdef CSD_EQ_DIAG_WITNESS
            unsigned p0, p1;
            pt :> p0;
#endif
            if (eng) CSD_ENGINE_CONFIG((int)csd_eq_cfg_idx_get());
            else     CSD_ENGINE_CLEAR();
            csd_eq_take_resync();     /* new engine epoch, clean pipeline   */
            csd_eq_engage(eng && (unsigned)CSD_ENGINE_READY());
#ifdef CSD_EQ_DIAG_WITNESS
            pt :> p1;
            g_csd_eq_pause_events++;
            { unsigned d = p1 - p0; if (d > g_csd_eq_pause_ticks_max) g_csd_eq_pause_ticks_max = d; }
#endif
        }
        g_csd_task_loops++;   /* REVERT(dp172) */
        if (g_csd_probe_mask & 16u) {   /* REVERT(dp201): task-loop stage
         * witness, bit4-gated per dp202 lean-gate (was an unconditional
         * timer read per task loop at idle ~0.8 M/s). */
            g_csd_eq_stage = 1u; g_csd_eq_stage_t = csd_ups_now(); }
        if (!csd_eq_take_ready()) {
            /* Idle backoff (XC select/timerafter): re-seed from the live
             * timer so a busy phase can never leave a stale deadline that
             * degrades into a spin; 100 ticks keeps fill latency well under
             * a frame while yielding the core to the rest of the tile. */
            t :> deadline;
            select { case t when timerafter(deadline + 100u) :> void: break; }
            continue;
        }
#ifdef CSD_EQ_DIAG_WITNESS
        timer eqt; unsigned e0, e1;
        eqt :> e0;
#endif
        g_csd_task_past_take++;   /* REVERT(dp172) */
        unsigned s[2];
        s[0] = (unsigned)csd_eq_take_l();
        s[1] = (unsigned)csd_eq_take_r();
        CSD_ENGINE_FRAME(s);
        csd_eq_take_commit(s[0], s[1]);
#ifdef CSD_EQ_DIAG_WITNESS
        eqt :> e1;
        g_csd_eq_frames++;
        { unsigned d = e1 - e0; if (d > g_csd_eq_ticks_max) g_csd_eq_ticks_max = d; }
#endif
#ifdef CSD_UPSAMPLE_EN
        /* Frames that reached the task were filtered here, so the resampler
         * client is fed AFTER the filter, in order, from here. */
        s_feedcalls_task++;
        csd_ups_feed((int)s[0], (int)s[1]);
#endif
    }
}
#endif /* !CSD_DSP_INLINE_EQ */

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
