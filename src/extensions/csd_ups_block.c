/* CSD upsampling engine -- one client of the DSP block engine.
 *
 * ONE block engine, TWO INDEPENDENT CLIENTS (owner directive 2026-11-10,
 * superseding the merged "the upsampler IS the DSP" doctrine that made the
 * audio DSP subordinate to the sample-rate converter): both this resampler
 * and the bass shelf run on lib_audio_dsp's fd_block_fir block engine
 * (AN02027), each with its own enable -- the resampler from trim /
 * DIAG:UPSAMPLing:STATe, the shelf from the DSP preset -- and neither
 * gating the other. The per-function-call engine that lived here before is
 * deleted and stays deleted.
 *
 * Polyphase identity: an R-times upsampler is R same-rate convolutions. Each
 * phase filter of the bank (csd_ups_block_conf.h, emitted by
 * tools/dsp/gen_ups_block.py through the vendor's fir_block_fd generator) is
 * fed the SAME host-rate input stream at frame_advance samples at a time; the
 * R outputs of a compute pass are de-interleaved into frame_advance*R wire
 * words. frame_advance is independent of the block length, which is what
 * makes a per-phase filter (few taps) satisfy the valid-output window
 * td_block_length + 1 - taps_per_block >= frame_advance.
 *
 * Cadence contract with lib_xua's audiohub loop (the loop that GENERATES
 * LRCLK in software -- nothing in it may block):
 *
 *   csd_ups_feed(l, r)  -- one host pair IN, at host-pair cadence (the weak
 *       UserBufferManagement hook, once every `ratio` LRCLK frames). Inputs
 *       land in the frame ring; when frame_advance of them have arrived, the
 *       whole polyphase bank computes and pushes one block of wire words.
 *
 *   csd_ups_slot(ch, w) -- one wire slot OUT, at LRCLK cadence (the audiohub
 *       I2S output loop). PURE POP: load, index, decrement, counter, return.
 *       Returns w unchanged when the path is not active; on an empty ring it
 *       repeats the last pushed word (an honest repeat, never stale memory).
 *
 * Words pass by value on purpose: no XC/C pointer-kind disagreement across
 * translation units, single machine word. The engine state has ONE writer --
 * the audio tile (frame hook + csd_AudioHwConfig); the diagnostic tile only
 * reads, plus csd_ups_set_want/set_enabled from SCPI commands, which is why
 * s_want goes through volatile-style publication like every other field the
 * audio thread reads.
 */
#include "csd_ups_block.h"
#include "csd_ups_block_conf.h"
#include <string.h>
#include <stdint.h>

/* XS3 local timer (10 ns ticks), read through the app's XC shim
 * (clearsounddac_board.xc: app C translation units cannot claim a timer
 * resource). Two reads per BLOCK (once per frame_advance host pairs), used
 * for the worst-block-cost witness the bench needs to judge the compute
 * burst against the LRCLK budget -- cost irrelevant at that cadence. */
extern unsigned csd_ups_now(void);

/* ---- option state (control plane) --------------------------------------
 * Runtime enable, DEFAULT OFF: shipping behaviour keeps the DAC's built-in
 * filter, the acoustically proven path. The MIDI/SCPI surface is the intended
 * way to turn it on. Enabled is what the codec ACCEPTED (its own oversampling
 * FIR bypassed); it does not mean active -- the path still has to be armed at
 * a family output rate, or the frame is left alone and refusals count. */
static unsigned s_enabled = 0u;
static unsigned s_refusals = 0u;

/* The operator's INTENT, kept on the tile that reads it. Deliberately
 * separate from s_enabled: want is what the journal says, enabled is what the
 * codec granted. Reading the persisted flash flag directly here would be a
 * cross-tile static read -- tile[1] fetches it over the channel at stream
 * start instead (csd_AudioHwConfig -> DAC_TRIM_UPS_GET). */
static volatile unsigned s_want = 0u;
void     csd_ups_set_want(unsigned w) { s_want = w ? 1u : 0u; }
unsigned csd_ups_get_want(void)       { return s_want; }

/* Family map -- THE one source of truth for "host rate -> forced family wire
 * rate". lib_xua's audiohub (ConfigAudioPorts fabric decision) and the board
 * (MCLK choice, forced-divider write) both read it here, so the interpolator
 * and the MCLK the codec sees cannot disagree; that agreement is what makes
 * an 8x rate error impossible rather than merely unlikely. */
unsigned csd_ups_expected_out_hz(unsigned in_hz)
{
    switch (in_hz) {
    case 48000u:
    case 96000u:
    case 192000u: return 384000u;    /* 48 series   -> 384 kHz   */
    case 44100u:
    case 88200u:
    case 176400u: return 352800u;    /* 44.1 series -> 352.8 kHz */
    default:      return 0u;         /* not a family member      */
    }
}

/* Integer ratio (8/4/2), 0 when the rate is unsupported -- never a silent
 * wrong answer. Both families share one table per ratio because the
 * prototype is normalised against its own input rate; only MCLK differs. */
static unsigned ratio_for(unsigned in_hz)
{
    switch (in_hz) {
    case 48000u:   case 44100u:   return 8u;
    case 96000u:   case 88200u:   return 4u;
    case 192000u:  case 176400u:  return 2u;
    default:       return 0u;
    }
}

/* ---- engine state -------------------------------------------------------
 * One fd_block_fir instance per (phase, channel); the phase filters of one
 * bank all share frame_advance. Sized for the largest ratio -- a smaller
 * bank simply uses the first R. The fd kernel does VPU (8-byte) accesses
 * through the blobs, so they must be 8-byte aligned. */
static int32_t         ups_blob[CSD_UPS_MAX_RATIO][2][CSD_UPS_MAX_DATA_WORDS]
                                 __attribute__((aligned(8)));
static fd_fir_data_t   ups_data[CSD_UPS_MAX_RATIO][2];
static fd_fir_filter_t ups_filter[CSD_UPS_MAX_RATIO];
/* The compute emits at most td_block_length + 1 samples; the valid window
 * starts at frame_overlap. +8 headroom, aligned like every fd buffer. */
static int32_t         ups_scratch[CSD_UPS_MAX_TD_BLOCK_LENGTH + 8]
                                 __attribute__((aligned(8)));

/* Input frame ring: frame_advance host pairs, then the bank computes. */
static int32_t         ups_in[2][CSD_UPS_FRAME_ADVANCE];
static unsigned        ups_fidx;

/* Wire delivery ring: one full block (frame_advance * max ratio) words per
 * channel, filled whole at the block boundary and drained one pop per wire
 * slot. Power-of-two size: the pop is a mask, never a divide. */
static int32_t         ups_q[2][CSD_UPS_RING_WORDS];
static unsigned        ups_rd[2], ups_occ[2];
static int32_t         ups_last[2];        /* last pushed word, underrun hold */

static unsigned        s_in_hz;            /* host stream rate          */
static unsigned        s_out_hz;           /* confirmed wire rate, 0 = never armed */
static unsigned        s_bypass;           /* 1 => path refused, frame untouched */
static unsigned        ups_phases;         /* active bank size R, 0 = none */
static unsigned        s_ratio_shift;      /* log2(R): stride in the push */

/* diagnostics */
static unsigned        s_frames;           /* host pairs accepted        */
static unsigned        s_rate_rejects;     /* non-family re-configs refused */
static unsigned        s_pops[2], s_repeats[2], s_occ_high[2];
static unsigned        s_blocks;           /* bank computes run          */
static unsigned        s_block_ticks_max;  /* worst compute burst, 10 ns ticks */
static unsigned        s_rate_calls, s_last_req_hz;

/* (Re)load the bank for the current host rate and clear all streaming
 * state. Every caller is control plane: stream start, rate change, arm,
 * enable flip -- so the fd init loops are allowed here, never in the data
 * path. A geometry mismatch leaves the bank unloaded (ups_phases = 0),
 * which refuses the frame rather than filtering at a wrong ratio. */
static void ups_reconfigure(void)
{
    const unsigned R = ratio_for(s_in_hz);

    ups_phases = 0u;
    memset(ups_in, 0, sizeof ups_in);
    memset(ups_q, 0, sizeof ups_q);
    ups_fidx = 0u;
    for (unsigned c = 0u; c < 2u; c++) {
        ups_rd[c] = 0u; ups_occ[c] = 0u; ups_last[c] = 0;
    }
    if (R < 2u || R > CSD_UPS_MAX_RATIO) return;

    const csd_ups_bank_t *bank = &csd_ups_banks[R / 2u - 1u];
    if (bank->num_phases != R) return;
    for (unsigned p = 0u; p < R; p++) {
        const csd_ups_phase_t *ph = &bank->phases[p];
        /* add_data/compute must run in lock-step at frame_advance
         * granularity; refuse a bank whose generated geometry disagrees. */
        if (ph->frame_advance != CSD_UPS_FRAME_ADVANCE) return;
        if (ph->data_words > CSD_UPS_MAX_DATA_WORDS) return;
        if (ph->td_block_length + 1u < ph->taps_per_block + ph->frame_advance)
            return;   /* valid-output window must cover frame_advance */
        ups_filter[p].coef_blocks     = (bfp_complex_s32_t *)ph->coef_blocks;
        ups_filter[p].td_block_length = ph->td_block_length;
        ups_filter[p].block_count     = ph->block_count;
        ups_filter[p].taps_per_block  = ph->taps_per_block;
        for (unsigned c = 0u; c < 2u; c++) {
            memset(ups_blob[p][c], 0, sizeof ups_blob[p][c]);
            fd_block_fir_data_init(&ups_data[p][c], ups_blob[p][c],
                                   ph->frame_advance, ph->td_block_length,
                                   ph->block_count);
        }
    }
    ups_phases = R;
    /* R is 2/4/8 from ratio_for(), so the stride is always a shift. */
    s_ratio_shift = (R == 8u) ? 3u : ((R == 4u) ? 2u : 1u);
}

/* ---- control plane (called from the audio tile's config path) ---------- */

/* Host stream rate. A change is a stream restart: history from the previous
 * rate is invalid, so it is dropped rather than filtered.
 *
 * Only a FAMILY rate may re-arm a live family stream. WHY: csd_AudioHwConfig
 * carries no direction flag and lib_xua re-invokes it for the 384 kHz
 * microphone stream as well as for playback; while armed, a non-family rate
 * (the mic's 384 kHz) is refused and counted rather than silently
 * disarming a playing stream. The mic rate is constant by design (same MCLK
 * as the DAC), so it must never drive the engine. */
void csd_ups_note_stream_rate(unsigned hz)
{
    s_rate_calls++;
    s_last_req_hz = hz;
    if (hz == s_in_hz) return;
    if (s_out_hz != 0u && !s_bypass && csd_ups_expected_out_hz(hz) == 0u) {
        s_rate_rejects++;
        return;
    }
    s_in_hz = hz;
    ups_reconfigure();
}

/* Confirm the real wire rate the board configured. Two families are
 * acceptable -- 384 kHz and 352.8 kHz -- but the value must belong to the
 * family the input rate is in: arming 44.1 kHz material at 384 kHz would be
 * a ratio of 8.708, not a filter setting, so the path is disabled rather
 * than run at a wrong rate. */
void csd_ups_note_wire_rate(unsigned out_hz)
{
    const unsigned want_out = csd_ups_expected_out_hz(s_in_hz);
    if (out_hz == 0u || (want_out != 0u && out_hz != want_out) ||
        (want_out == 0u && out_hz != 384000u && out_hz != 352800u)) {
        if (s_out_hz != 0u || !s_bypass) {
            s_out_hz = 0u; s_bypass = 1u; ups_reconfigure();
        }
        return;
    }
    if (s_out_hz != out_hz) {
        s_out_hz = out_hz; s_bypass = 0u;
        ups_reconfigure();
    }
}

void csd_ups_set_enabled(unsigned enable)   /* not 'on': reserved in XC */
{
    const unsigned now = enable ? 1u : 0u;
    if (now != s_enabled) {
        s_enabled = now;
        if (!now) {
            /* Turning the resampler CLIENT off disarms it too: an armed-but-
             * disabled engine lied on DIAG:UPSAMPLing:INFO? (armed=1 while the
             * operator had just written STATe 0) and let the board's
             * `!csd_ups_armed()` guards skip the enabled/pacer re-apply on a
             * non-family re-config. The next legitimate arm re-runs
             * csd_ups_note_wire_rate from the board's config path. */
            s_out_hz = 0u;
            s_bypass = 1u;
        }
        ups_reconfigure();          /* a mode switch is a stream restart */
    }
}
unsigned csd_ups_is_enabled(void) { return s_enabled; }

/* ---- data plane ---------------------------------------------------------
 */

/* 1 when the engine owns the wire words: the operator enabled the client
 * (want), the codec granted the OSF bypass (enabled), the wire really runs
 * at the family rate (armed, no refusal) and a bank is loaded. The DSP
 * client is NOT gated on this (owner directive 2026-11-10, one block engine
 * / two independent clients): csd_dsp_frame runs the shelf on every host
 * rate and feeds the pair here; csd_ups_slot pops only when this is 1. */
unsigned csd_ups_active(void)
{
    return (s_want && s_enabled && s_out_hz != 0u && !s_bypass &&
            ups_phases != 0u)
           ? 1u : 0u;
}

unsigned csd_ups_armed(void)      /* armed regardless of the codec grant */
{
    /* want is part of arming: the resampler client exists only where the
     * operator's own enable (DIAG:UPSAMPLing:STATe / trim) put it. With want
     * cleared, every armed read goes 0, so no guard or diagnostic can act on
     * a resampler the operator just switched off. */
    return (s_want && s_out_hz != 0u && !s_bypass) ? 1u : 0u;
}

/* One host pair in, at host-pair cadence. The frame is left untouched --
 * the wire words come out of csd_ups_slot at LRCLK cadence, which is the
 * only way to span the host-rate/wire-rate gap without writing ratio words
 * into a host-cadence buffer. Balance proof: one push of frame_advance*R
 * words per channel per block; the audiohub loop pops one word per channel
 * per LRCLK, and one pair arrives every R LRCLKs, so the ring returns to
 * empty exactly as the next block is pushed. */
void csd_ups_feed(int l, int r)
{
    const unsigned R = ups_phases;

    if (!s_enabled || !s_want) return;           /* client off: nothing to do */
    if (s_bypass || s_out_hz == 0u || R == 0u) {
        s_refusals++;                          /* on but refused: count, no queue */
        return;
    }

    ups_in[0][ups_fidx] = (int32_t)l;
    ups_in[1][ups_fidx] = (int32_t)r;
    if (++ups_fidx < CSD_UPS_FRAME_ADVANCE) return;
    ups_fidx = 0u;

    /* Block boundary: run the whole polyphase bank. This is the compute
     * burst -- amortised over frame_advance * R wire frames (1024 slots at
     * ratio 8), NOT per slot. The witness counters below exist so the bench
     * can judge it against the LRCLK budget over DIAG:UPSAMPLing:INFO?. */
    const unsigned t0 = csd_ups_now();
    const csd_ups_bank_t *bank = &csd_ups_banks[R / 2u - 1u];
    const unsigned shift = s_ratio_shift;
    for (unsigned c = 0u; c < 2u; c++) {
        int32_t *q = ups_q[c];
        for (unsigned p = 0u; p < R; p++) {
            fd_block_fir_add_data(ups_in[c], &ups_data[p][c]);
            fd_block_fir_compute(ups_scratch, &ups_data[p][c], &ups_filter[p]);
            /* The valid frame is [frame_overlap, frame_overlap +
             * frame_advance) of the compute output -- the same window
             * csd_dsp_fd.c uses; de-interleaved to wire order: input step j,
             * phase p -> wire word (j << shift) + p. */
            const int32_t *v = &ups_scratch[bank->phases[p].frame_overlap];
            for (unsigned j = 0u; j < CSD_UPS_FRAME_ADVANCE; j++)
                q[(j << shift) + p] = v[j];
        }
        ups_rd[c] = 0u;
        ups_occ[c] = CSD_UPS_RING_WORDS;
        ups_last[c] = q[CSD_UPS_RING_WORDS - 1u];
        if (s_occ_high[c] < CSD_UPS_RING_WORDS)
            s_occ_high[c] = CSD_UPS_RING_WORDS;
    }
    const unsigned dt = csd_ups_now() - t0;
    if (dt > s_block_ticks_max) s_block_ticks_max = dt;
    s_blocks++;
    s_frames++;
}

/* One wire slot out, at LRCLK cadence (called from the audiohub loop, one
 * channel per slot). PURE POP -- one load, one index update, one decrement,
 * one counter, one return: no compute of any kind runs at slot cadence,
 * because LRCLK is generated in software by the loop this function sits in.
 * Empty ring means the host stalled: hold the last pushed word rather than
 * emit stale ring memory -- an honest repeat, never a lie. */
int csd_ups_slot(unsigned ch, int w)
{
    if (!csd_ups_active()) return w;   /* not active: word untouched */
    if (ch >= 2u)          return w;   /* not our channel */

    if (ups_occ[ch] == 0u) {
        s_repeats[ch]++;               /* starvation, visible on the host */
        return (int)ups_last[ch];
    }
    const int32_t v = ups_q[ch][ups_rd[ch]];
    ups_rd[ch] = (ups_rd[ch] + 1u) & CSD_UPS_RING_MASK;
    ups_occ[ch]--;
    s_pops[ch]++;
    return (int)v;
}

/* ---- diagnostics (read-only views for the SCPI/MIDI surface) ----------- */

unsigned csd_ups_diag_frames(void)  { return s_frames; }
unsigned csd_ups_diag_in_hz(void)   { return s_in_hz; }
unsigned csd_ups_diag_out_hz(void)  { return s_out_hz; }
unsigned csd_ups_diag_refusals(void){ return s_refusals; }
unsigned csd_ups_diag_rate_rejects(void) { return s_rate_rejects; }
unsigned csd_ups_diag_pops(unsigned ch)     { return (ch < 2u) ? s_pops[ch] : 0u; }
unsigned csd_ups_diag_repeats(unsigned ch)  { return (ch < 2u) ? s_repeats[ch] : 0u; }
unsigned csd_ups_diag_occ_high(unsigned ch) { return (ch < 2u) ? s_occ_high[ch] : 0u; }
/* Block-engine witnesses: computes run, worst burst (10 ns ticks -- compare
 * against ratio * (600 MHz / wire rate) * frame_advance for the per-block
 * budget), engine rate hand-overs, and the last rate requested. */
unsigned csd_ups_diag_blocks(void)          { return s_blocks; }
unsigned csd_ups_diag_block_ticks_max(void) { return s_block_ticks_max; }
unsigned csd_ups_diag_rate_calls(void)      { return s_rate_calls; }
unsigned csd_ups_diag_last_req_hz(void)     { return s_last_req_hz; }
