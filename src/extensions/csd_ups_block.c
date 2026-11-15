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
#include "csd_eq_ring.h"   /* 11.11.39: arm posts a task re-adoption */
#include <string.h>
#include <stdint.h>
#include <xs1.h>   /* dp93: get_local_tile_id witness */
#include <stdio.h>
#include <stdlib.h>

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
/* Cold-path publisher for the armed-mic feed mailbox in csd_dsp.xc (see the
 * feed block there): every gate input (s_want/s_enabled/s_out_hz/s_bypass/
 * ups_phases/s_tap) mutates only behind a function that calls this, so the
 * UBM hot path never reaches into this file. Definition below (needs
 * csd_ups_active). REVERT with the feed. */
static void csd_ups_push_feed_owner(void);
static void ups_reconfigure(unsigned site); /* wrapper defined with the publisher */
/* REVERT(s15-diag): per-site reconfigure attribution (periodic ring-reset hunt). */
static unsigned s_rc_total, s_rc_site[4], s_rc_last;
/* EXPERIMENT 11.11.34: arm-generation handoff (REVERT with the tap).
 * ups_fidx survives across streams (only ups_reconfigure resets it), so a
 * newly armed stream started its 128-frame block grid at an unknown phase
 * — the oracle could not reproduce it. The AUDIO core performs the reset
 * itself on the first armed feed after each arm (single-writer preserved:
 * SCPI only bumps the generation counter). */
static volatile unsigned s_arm_gen  = 0u;
static unsigned          s_arm_seen = 0xFFFFFFFFu;

void     csd_ups_set_want(unsigned w) { s_want = w ? 1u : 0u;
                                        if (w) s_arm_gen++;
                                        /* 11.11.39 (REVERT-tagged bisect): every
                                         * arm/disarm posts a no-op EQ config so
                                         * the task runs its full PAUSE -> drain
                                         * -> RESUME -> engage adoption: the
                                         * consumer cursors rebase to the
                                         * producer, no stale backlog can outlive
                                         * a stream, and a starved/mis-cursored
                                         * task is revived. Same path a PRESet
                                         * change uses; stream-safe by design. */
                                        csd_eq_cfg_post(csd_eq_cfg_on_get(),
                                                        csd_eq_cfg_idx_get());
                                        csd_ups_push_feed_owner(); }
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
/* dp121 REVERT: shared-heap engine state. On this board the malloc heap
 * is the only memory proven visible to EVERY core (probe rings migrated
 * there in dp77 and immediately became readable by the diag core); plain
 * statics were per-core views — the root cause of the dp113-dp119 capture
 * blindness (same address, different bytes). Array-pointer typedefs keep
 * every ups_q[c][i] / ups_blob[p][c][i] usage unchanged. */
static int32_t         (*ups_blob)[2][CSD_UPS_MAX_DATA_WORDS];
static fd_fir_data_t   (*ups_data)[2];
static fd_fir_filter_t *ups_filter;
/* The compute emits at most td_block_length + 1 samples; the valid window
 * starts at frame_overlap. +8 headroom, aligned like every fd buffer. */
static int32_t         *ups_scratch;   /* heap, 8-aligned */

/* Input frame ring: frame_advance host pairs, then the bank computes. */
static int32_t         (*ups_in)[CSD_UPS_FRAME_ADVANCE];
#define ups_fidx  (ups_st->fidx)

/* Wire delivery ring: TWO blocks (frame_advance * max ratio, doubled --
 * see csd_ups_block_conf.h) words per channel. Blocks are pushed whole
 * into alternating halves at the block boundary and drained one pop per
 * wire slot, so the pop always has the previous block while the next
 * computes (session 14). Power-of-two size: the pop is a mask, never a
 * divide. */
/* Session 17c: TWO delivery rings. The CERTIFIED ring is the committed
 * 2 x max-ratio-block ring (2048 words/ch) and sits at the head of the one
 * heap block, exactly where the committed image had it -- every ratio below
 * MAX streams through it with the committed mask, base and seed, and every
 * other region keeps its committed offset. The EXTENDED ring (2 x
 * CSD_UPS_RING_WORDS) is appended at the END of the allocation and is used
 * ONLY at R == CSD_UPS_MAX_RATIO, the one ratio that needs four blocks in
 * flight. Growing the physical ring in place perturbed ratio 4 (a shared
 * heap-layout coupling, measured across three images), so R=8's growth must
 * not move anything R<8 reads. */
#define CSD_UPS_RING_CERT_WORDS (2u * CSD_UPS_FRAME_ADVANCE * CSD_UPS_MAX_RATIO)
static int32_t         (*ups_q)[CSD_UPS_RING_WORDS];
static int32_t         (*ups_q_cert)[CSD_UPS_RING_CERT_WORDS];
static int32_t          *ups_qp[2];   /* ACTIVE per-channel ring base */
/* pushn: blocks committed since the last ring reset (double-buffer region
 * parity + the pop-side lead gate). pushov: capacity-backoff bound hits.
 * HEAP, not static: the pop-side reader runs on the wire/producer core,
 * the committing writer on the EQ task core -- plain statics across that
 * boundary are exactly the dp121 per-core-view class (only the malloc heap
 * is proven visible to EVERY core). */
typedef struct { unsigned fidx, phases, rd[2], occ[2]; int32_t last[2];
                 unsigned pushn, pushov; } ups_st_t;
static ups_st_t        *ups_st;   /* shared-heap scalars (dp121) */
#define ups_rd    (ups_st->rd)
#define ups_pushn (ups_st->pushn)
#define ups_pushov (ups_st->pushov)
#define ups_occ   (ups_st->occ)
#define ups_last  (ups_st->last)

static unsigned        s_in_hz;            /* host stream rate          */
static unsigned        s_out_hz;           /* confirmed wire rate, 0 = never armed */
static unsigned        s_bypass;           /* 1 => path refused, frame untouched */
#define ups_phases (ups_st->phases)       /* active bank size R, 0 = none */
static unsigned        s_ratio_shift;      /* log2(R): stride in the push */

/* diagnostics */
static unsigned        s_frames;           /* host pairs accepted        */
static unsigned        s_rate_rejects;     /* non-family re-configs refused */
/* dp149 REVERT: volatile pops -- the qpop1 recorder on core A reads
 * s_pops[1] (written by core B) once, at accept, to base its row map. */
static volatile unsigned s_pops[2];
static volatile unsigned s_repeats[2];   /* dp151: pair-complete check reads both */
static unsigned        s_occ_high[2];
/* dp149 REVERT: per-channel qpop1 row bases (s_pops values at accept).
 * The dp95-era row (s_probe_pop_n-1, a ch1-only pair counter) let core A's
 * ch0 word land on core B's commit frontier: whenever a feed claim held
 * both pops at a boundary (dp147 halt), ch0's write lagged ch1's commit
 * by one and the WHOLE ch0 array re-skewed +1 pair from there (jd1: rows
 * 0..105 exact, row 106 = pair 107's word — the 'oracle boundary 1 pop
 * late' red herring; the engine was bit-exact, the witness lied). Each
 * channel now rows its own consumption counter, immune to cross-core
 * ordering. Rows count CONSUMING pops; a starved pair (repeat) leaves no
 * row on either channel, so rows stay pair-aligned. Volatile: written by
 * the claiming ch1 core, read by both cores' recorder paths. */
static volatile unsigned s_qpop1_base[2];
/* dp150 REVERT: row-map diagnostics — s_pops/pop_n snapshots at the claim
 * (core B) and at the accept reset (core A), exported BASE fields 97..103. */
volatile unsigned s_diag_qb[2];      /* s_qpop1_base at claim */
volatile unsigned s_diag_pops_claim[2];
volatile unsigned s_diag_popn_claim, s_diag_kabs_claim;
volatile unsigned s_diag_pops_accept[2], s_diag_popn_accept;
static unsigned        s_blocks;           /* bank computes run          */
/* Session 14 (2026-11-14) double-buffered delivery: blocks alternate
 * between the two halves of the (2 x FA x MAX_RATIO)-word ring, so the
 * pop stream always has the previous block to drain while the next one
 * computes. The counters live in ups_st (heap): pop-side reader core vs
 * committing writer core -- dp121 rule, plain statics are NOT shared. */
static unsigned        s_block_ticks_max;  /* worst compute burst, 10 ns ticks */
static unsigned        s_rate_calls, s_last_req_hz;
/* REVERT(s16-diag): block-granularity ring witnesses (ratio-4 zero-run hunt).
 * Pop side (producer core): zero-valued popped words, cumulative and bucketed
 * per pop-block period (fill pops) into a last-4 histogram — distinguishes a
 * block-PHASED zero cycle (region/parity defect) from uniform loss. Push side
 * (task core, block cadence): last commit's base, occ after commit, and a
 * stale-region-overwrite count (first word of the target region non-zero when
 * the pop stream claims it consumed) — classifies zeros never-pushed vs
 * pushed-then-overwritten-early. All PROBE-bit5 gated; read via INFO tail. */
volatile unsigned s16_popz[2];         /* zero-valued pops, cumulative     */
volatile unsigned s16_lgg[2], s16_lggz[2]; /* lead-gate holds / zero holds */
volatile unsigned s16_blkz[2][4];      /* [ch][age]: zero pops, last 4 pop-blocks */
static    unsigned s16_blkcnt[2], s16_blkph[2];
volatile unsigned s16_pbase, s16_pocc[2], s16_ovlv;
/* Session 16 close-out (dp204 lean-gate rule): ONE probe-mask read per
 * producer pass, taken in csd_ps_produce_pair(); every gated site on the
 * pop path tests this snapshot. The pop path carried ~7 independent
 * g_csd_probe_mask loads per pass (2 pops x lead/halt/ifed/popz) -- pure
 * shared-region traffic inside the 260.4 t wire-stride budget. Staleness
 * is one pass (witnesses only). */
volatile unsigned s_pass_pm;
/* Session 16 close-out: hold-last is PRODUCER-side state (writer = pop
 * side, readers = pop empty branch + lead gate, same core). Keeping it off
 * the shared heap removes one RMW per pop; semantics unchanged (last
 * POPPED live word, honest repeat). */
static int32_t s_ups_last[2];

/* Session 17: pop-side LEAD seed, expressed in BLOCKS of the effective ring.
 * The invariant is the reserve at the point where the capacity guard admits
 * the next commit: at R = MAX one block is exactly a quarter of the ring, so
 * the session-16 rule (seed 3 there, 2 below) still holds and every ratio
 * keeps its certified behaviour -- see ups_ring_geometry. */
static unsigned ups_ring_w = CSD_UPS_RING_WORDS;   /* EFFECTIVE ring words  */
static unsigned ups_ring_mask = CSD_UPS_RING_WORDS - 1u;

/* Session 17b: the physical ring is sized for the DEEPEST ratio (4 x MAX
 * blocks -- the fix48 anti-starvation geometry), but the ring a given ratio
 * streams through is the LARGER of the session-14 minimum (two MAX-ratio
 * blocks, 2048 words) and four blocks of THAT ratio. Every site that indexes
 * or bounds the ring (push base, pop cursor, capacity guard, seed) uses the
 * effective values, so a ratio below MAX streams through exactly the geometry
 * it was certified at -- enlarging the physical ring must not change it.
 * Measured (image 74dd4278, armed 96k = ratio 4): with the full 4 x MAX ring
 * the ratio-4 leg regressed to one whole 128-frame block of hold-last per ~7
 * blocks (repsL 65k/s, maxrun 128, 375*ratio comb, peak 960.6); the SAME
 * source at the 2048-word ring (old image 18f1d44e) was clean (repsL 0.4/s).
 * Seed, cap and fill were numerically identical in both -- only the ring
 * words differed -- hence the per-ratio effective ring. At MAX the effective
 * ring is the whole physical ring, so the fix48 behaviour is untouched. */
static void ups_ring_select(void)
{
    if (!ups_q || !ups_q_cert) return;         /* pre-alloc */
    for (unsigned c = 0u; c < 2u; c++)
        ups_qp[c] = (ups_ring_w > CSD_UPS_RING_CERT_WORDS)
                  ? &ups_q[c][0] : &ups_q_cert[c][0];
}

static void ups_ring_geometry(unsigned R)
{
    const unsigned minw = 2u * CSD_UPS_FRAME_ADVANCE * CSD_UPS_MAX_RATIO;
    const unsigned four = 4u * CSD_UPS_FRAME_ADVANCE * (R ? R : 1u);
    unsigned w = (four > minw) ? four : minw;
    if (w > CSD_UPS_RING_WORDS) w = CSD_UPS_RING_WORDS;
    ups_ring_w = w;              /* always a power of two, so the mask holds */
    ups_ring_mask = w - 1u;
    ups_ring_select();
}

/* Seed in BLOCKS of the effective ring, with the session-16 rule (seed 3
 * where one block is >= eff/4 -- i.e. at R = MAX, whose ring is exactly four
 * blocks -- and 2 elsewhere). Certified seeds: R=8 -> 3 (fix48), R=4 -> 3,
 * R=2 -> 2, R=1 -> 2. */
static unsigned ups_lead_seed(void)
{
    const unsigned fb = CSD_UPS_FRAME_ADVANCE * ups_phases;
    if (fb == 0u) return 2u;
    const unsigned maxb = ups_ring_w / fb;
    return (maxb >= 3u && fb >= ups_ring_w / 4u) ? 3u : 2u;
}

/* (Re)load the bank for the current host rate and clear all streaming
 * state. Every caller is control plane: stream start, rate change, arm,
 * enable flip -- so the fd init loops are allowed here, never in the data
 * path. A geometry mismatch leaves the bank unloaded (ups_phases = 0),
 * which refuses the frame rather than filtering at a wrong ratio. */
/* probe statics defined at EOF (DP-window placement); visible here */
extern volatile unsigned s_probe_on, s_probe_base, s_probe_popbase;
extern volatile unsigned s_rec_on;
extern volatile unsigned s_probe_reconfigs;
extern volatile unsigned s_probe_pre;
extern volatile unsigned s_probe_fidx;
/* Per-producer event rings (2026-11-12): the shared counter lost ~2% of
 * events (measured eh2: 43 non-consecutive steps per producer) because
 * BOTH producers did read/increment/store on one counter — the same
 * dual-writer class this campaign keeps killing. One counter per producer
 * core, merged host-side by timestamp. REVERT with the tap. */
extern volatile unsigned *s_probe_fev_t;      /* feed ev: stamp    */
extern volatile unsigned s_probe_fev_tot;     /* task-core writer only */
extern volatile unsigned s_pop_clock;         /* audio core: last pop idx */
extern volatile unsigned s_probe_prepop;      /* pops served before trigger */
static volatile unsigned s_snap_wanted;   /* pop-side capture armed at trigger */
volatile unsigned s_probe_ring_pre_freeze_crc, s_probe_ring_pre_freeze_nz; /* dp117 REVERT */
volatile unsigned s_snap_noowner;
/* dp118 REVERT: writer-core capture. The TASK core owns every byte the
 * snapshot needs (push writes the ring, compute writes the blobs, feed
 * writes the window) and sees its own writes by construction; the pop
 * core merely claims the trigger and is halted while the task copies. */
volatile unsigned s_snap_claimed, s_pop_halt, s_pop_in_commit;
/* REVERT(dp201 2026-11-13): producer-path attribution witnesses, PROBE
 * bit4 gated (10 ns ticks, accumulate-since-boot; host takes fixed-window
 * deltas). Hunt: p_tdl ~22M t per 256-pass window (~870 us/pass) with the
 * producer at ~80 pass/s while the wire is port-locked. dp200 moved the
 * UBM feed onto the producer thread; the s_in_feed spin in pop_active is
 * now cross-core (tile[1] EQ task owns feed()). PACE? fields 31-42 print
 * these; the _n columns count events whose region exceeded 64 t. */
unsigned g_csd_p_halt = 0, g_csd_p_halt_n = 0;   /* s_pop_halt spin        */
unsigned g_csd_p_ifed = 0, g_csd_p_ifed_n = 0;   /* s_in_feed spin at pop  */
unsigned g_csd_p_snap = 0, g_csd_p_snap_n = 0;   /* in-pop snapshot capture */
unsigned g_csd_p_claim = 0, g_csd_p_claim_n = 0; /* feed() claim loop      */
extern unsigned g_csd_probe_mask;                /* xua_audiohub.xc        */
/* REVERT(dp201): task-side freeze attribution (tile[1] EQ task). stage:
 * 1=top, 2=claim loop, 3=engine frame, 4=feed body, 5=block compute.
 * stage_t: ticks since stage entry (host reads it frozen at the stuck
 * stage). claim_rounds: total outer claim rounds entered; claim_exit:
 * claim completions (rounds==exits => never stuck). live_* snapshot the
 * handshake flags at each stage transition. */
unsigned g_csd_eq_stage = 0, g_csd_eq_stage_t = 0;
unsigned g_csd_eq_claim_rounds = 0, g_csd_eq_claim_exit = 0;
unsigned g_csd_live_pop_in_commit = 0, g_csd_live_snap_freeze = 0;
unsigned g_csd_live_in_feed = 0, g_csd_live_probe_popn = 0;
extern unsigned csd_ups_now(void);
volatile unsigned s_witness_sp_slot, s_witness_sp_feed, s_witness_sp_snap; /* dp120 */
volatile unsigned s_witness_stptr, s_witness_heapok, s_witness_fidx_cap, s_witness_winw0; /* dp122 */
volatile unsigned s_snap_kabs, s_pop_clock_reset;
volatile unsigned s_wit_g[4];   /* dp135: gate cursor snapshot at last pass */
volatile unsigned s_snap_accepts;  /* dp135: accepted captures */
volatile unsigned s_snap_groups;   /* dp131: pop-groups spent chasing an accept */
volatile unsigned s_snap_crc_capture;  /* dp130: crc of ring section at accept (capture core) */
volatile unsigned s_snap_crc_diag;     /* dp130: crc latched by the diag EV reader */
volatile unsigned s_snap_progress;   /* dp129: how far the last pass got */
volatile unsigned s_snap_entry;      /* dp129: pass entries */   /* dp114b: pops that saw a zero ring at the capture gate */
#define CSD_SNAP_DUMP 520u   /* words of blob per bank: covers
                              * headers+bfp+td+prev_td+overlap (518 max) */
#define CSD_SNAP_PER_BANK (CSD_SNAP_DUMP + 5u)  /* head, dump, td, bc, fa, pad */
#define CSD_SNAP_RINGW (2u * CSD_UPS_RING_WORDS) /* delivery ring c-major */
volatile unsigned s_probe_snap_body_crc, s_probe_snap_body_nz;         /* dp111 REVERT */
volatile unsigned s_probe_ring_direct_crc, s_probe_ring_direct_nz;    /* dp113 REVERT */
/* dp93 fq1: the feed side (task core) keeps writing fidx/ups_in/blob while
 * the pop-side snapshot copies — witnesses caught ups_fidx moving 71->99
 * INSIDE the capture branch. There is no single-core instant for a
 * two-core engine: capture runs under a stop-the-world where the feed
 * core pauses at its entry point (one volatile load when unarmed). */
volatile unsigned s_snap_freeze;          /* audio sets; task spins on it */
volatile unsigned s_snap_qaddr, s_snap_tile;   /* snapshot-side witnesses */
volatile unsigned s_feed_qaddr, s_feed_tile;   /* feed-side witnesses    */
volatile unsigned s_in_feed;              /* task: 1 while inside feed()   */
extern volatile unsigned s_feed_abs;          /* absolute feeds since PROBE */
extern volatile unsigned *csd_probe_snap;              /* trigger-time engine state  */
void csd_probe_snapshot(void);
unsigned csd_probe_snap_word(unsigned i);   /* fwd (defined below) */
extern volatile unsigned csd_probe_snap_words;
extern volatile unsigned *s_probe_pev_t;      /* pop ev: stamp     */
extern volatile unsigned s_probe_pev_tot;     /* audio-core writer only */
extern volatile unsigned s_abs_feed, s_abs_calls;
extern volatile unsigned s_probe_pop_n, s_probe_feed_n;
/* dp90 fn: the 34.6KB snapshot static + 192KB rings over-subscribed the
 * device heap (en_stat=2, the ONLY honest witness in the enable path —
 * TRUTH #9 again). 4096-row windows are 8x the scored requirement. */
#define CSD_PROBE_RING 4224u          /* dp157: 4096 exactly at teardown -> last pop row lost its c1 qpop1 write; +128 drain rows */
#define CSD_PROBE_FRING 4608u         /* absolute feed history — dp155: (dp155: 4352 missed the final scored boundary — feed row 4348 + window needs 4476) — dp154
                                       * REVERT: +256 rows so the last
                                       * scored pop (row 4095, stamped
                                       * 4231) has its boundary feed
                                       * (row 4220) payloads captured */
static unsigned (*s_probe_qpop1_buf)[CSD_PROBE_RING];  /* dp121: heap */
volatile unsigned *s_probe_qpop1;                       /* = buf[1] after alloc */
volatile unsigned s_probe_mis0, s_probe_mis1;  /* pop-vs-ring mismatches */
volatile unsigned s_probe_preblocks;           /* s_blocks at origin     */
unsigned csd_probe_snap_calls;                 /* fwd decl (defined below)*/
unsigned s_probe_blkpop[8];            /* pop clock at the first 8 boundaries */
unsigned s_probe_blk_n;                /* boundaries recorded                  */
unsigned s_probe_c0pop;                /* pop clock between c0 and c1 push     */
unsigned s_probe_reconfpop = 999999u;  /* pop clock when reconfigure fired  */
unsigned s_probe_qwpop[64];            /* pop clock of first 64 ring writes */
unsigned s_probe_qw_n;
unsigned s_probe_headc = 999999u;      /* head_index seen at compute entry  */
/* dp144 REVERT: post-trigger first-boundary witnesses. The row-122
 * divergence needs the device's OWN view at the first compute after the
 * capture accept: which feed count ran the compute, what the input
 * windows held (FNV crcs), and what the compute produced at the push
 * window (scratch[fa+{0..3,15}]). Latched for the first TWO post-accept
 * boundaries so the cadence itself is checkable. Compare against the
 * oracle's BND print (same formula, same instants). */
unsigned s_probe_bnd_feed[2], s_probe_bnd_pop[2];
unsigned s_probe_bnd_wc0[2], s_probe_bnd_wc1[2];
unsigned s_probe_bnd_head[2], s_probe_bnd_fa[2] = {999u, 999u};
unsigned s_probe_bnd_s0[2], s_probe_bnd_s1[2], s_probe_bnd_s2[2];
unsigned s_probe_bnd_s3[2], s_probe_bnd_s15[2];
unsigned s_probe_bnd_done;
/* dp146 REVERT: bisection witnesses -- 32-word FNV crcs of the PUSH
 * window scratch[fa..fa+128) (4 blocks) and an FNV crc of the whole
 * data block compute read (post add_data), per post-accept boundary. */
unsigned s_probe_bnd_scrc[2][4], s_probe_bnd_bcrc[2];
/* dp146 REVERT: alignment-class witnesses (low 20 bits are enough to
 * compare mod-64 classes between device heap and xsim heap). */
unsigned s_probe_bnd_pblob, s_probe_bnd_pdata, s_probe_bnd_pscratch;
unsigned s_probe_datac = 0u;           /* blob[0][0][0] at compute entry    */
unsigned s_probe_feedoff;              /* payload rows already in the seed  */unsigned s_probe_wchk;                   /* window checksum, task-maintained */
unsigned s_probe_wchk_snap;              /* window checksum, pop core at snap  */
unsigned s_probe_wchk_run;               /* running checksum at same instant */
unsigned s_feedcalls_task;               /* csd_ups_feed calls from EQ task   */
unsigned s_feedcalls_audio;              /* csd_ups_feed calls from audio path */
#define CSD_PROBE_EV 4608u            /* dp157: feed events outgrew 4096 with FRING 4608 */
extern volatile int32_t (*s_probe_pop)[2];
extern volatile int32_t (*s_probe_feed)[2];
/* dp121 REVERT: shared-heap allocator + state entry. malloc'd blocks are
 * the board's cross-core-visible memory; every block is CONTENT-verified
 * (write/read pattern) before use — TRUTH #9: a non-NULL pointer is not
 * a buffer. 8-byte aligned for fd VPU access. */
volatile unsigned ups_heap_fail;
static unsigned ups_heap_ok;
static void *ups_alloc(unsigned bytes)
{
    unsigned char *raw = (unsigned char *)malloc(bytes + 8u);
    if (!raw) return 0;
    unsigned char *a = (unsigned char *)(((unsigned)raw + 7u) & ~7u);
    volatile unsigned *vw = (volatile unsigned *)a;
    const unsigned n = bytes / 4u;
    for (unsigned i = 0u; i < n; i++) vw[i] = 0xA5A50000u ^ i;
    for (unsigned i = 0u; i < n; i++)
        if (vw[i] != (0xA5A50000u ^ i)) { free(raw); ups_heap_fail++; return 0; }
    memset(a, 0, bytes);
    return a;
}
/* dp123 REVERT: ONE block for all engine state, partitioned by hand.
 * The board's heap allocator hands out overlapping blocks for the 4th+
 * malloc (dp79; ht1: a probe buffer silently overlapped a live stamp
 * ring and produced the partial-zero-header capture). A single
 * allocation cannot self-overlap. */
/* dp124 REVERT: heap budget forced the allocation domain to the ratios
 * certification exercises (R<=2). Session 14 (2026-11-14) extends the
 * domain to R=8 for the ratio-4/8 armed gates; en_stat + ups_heap_total
 * witnesses prove the heap fits on-device (a NULL would free-and-refuse,
 * never a partial state). */
#define UPS_ALLOC_RATIO 8u
#define UPS_BLOB_WORDS ((unsigned)UPS_ALLOC_RATIO * 2u * (CSD_UPS_MAX_DATA_WORDS + 640u))
#define UPS_SCAL 16u
static unsigned *ups_mega;   /* raw handle (REVERT with the block) */
volatile unsigned ups_heap_total;   /* dp126 witness: words requested */
/* dp143: 4-word (16B) bump. The old even-word bump left region 8-byte
 * alignment dependent on the heap base parity: fine on the device, but
 * the xsim oracle heap can place scratch at 4 mod 8 -> vect compute
 * garbage (row-122 corruption, all words = 0x10 mod 2^20). The device
 * base was already 8-aligned, so shipping behavior is unchanged. */
static unsigned ups_bump(unsigned w) { return (w + 3u) & ~3u; }
static void ups_state_alloc(void)
{
    if (ups_heap_ok) return;
    const unsigned wq = 2u * CSD_UPS_RING_WORDS;       /* R==MAX ring */
    const unsigned wqc = 2u * CSD_UPS_RING_CERT_WORDS; /* certified    */
    const unsigned wi = 2u * CSD_UPS_FRAME_ADVANCE;
    const unsigned wb = UPS_BLOB_WORDS;
    const unsigned wd = (unsigned)UPS_ALLOC_RATIO * 2u
                        * (unsigned)(sizeof **ups_data / 4u);
    const unsigned wf = (unsigned)UPS_ALLOC_RATIO
                        * (unsigned)(sizeof *ups_filter / 4u);
    const unsigned ws = CSD_UPS_MAX_TD_BLOCK_LENGTH + 8u;
    /* Session 17b: the delivery ring is allocated LAST on purpose. It is the
     * only region whose size scales with the physical ring (2 x RING_WORDS),
     * and it is the engine's only cross-task (producer pop vs EQ task feed)
     * shared state. Growing it in place shifted every compute buffer's
     * address by 16 KB and measurably perturbed the pop/feed phase at ratio 4
     * (see ups_ring_geometry); keeping the compute regions at their certified
     * offsets leaves that geometry bit-identical to the image being fixed. */
    unsigned total = 0u;
    const unsigned oqc = total; total = ups_bump(total + wqc);
    const unsigned oi = total;  total = ups_bump(total + wi);
    const unsigned ob = total;  total = ups_bump(total + wb);
    const unsigned oda = total; total = ups_bump(total + wd);
    const unsigned of_ = total; total = ups_bump(total + wf);
    const unsigned osc = total; total = ups_bump(total + ws);
    const unsigned ost = total; total = ups_bump(total + UPS_SCAL);
    const unsigned oq = total;  total = ups_bump(total + wq);
    ups_heap_total = total;               /* dp126 witness */
    unsigned *m = ups_alloc(total * 4u);
    if (!m) { ups_heap_fail++; return; }
    ups_mega = m;
    ups_q_cert = (int32_t (*)[CSD_UPS_RING_CERT_WORDS])(m + oqc);
    ups_q = (int32_t (*)[CSD_UPS_RING_WORDS])(m + oq);
    ups_in = (int32_t (*)[CSD_UPS_FRAME_ADVANCE])(m + oi);
    ups_blob = (int32_t (*)[2][CSD_UPS_MAX_DATA_WORDS])(m + ob);
    ups_data = (fd_fir_data_t (*)[2])(m + oda);
    ups_filter = (fd_fir_filter_t *)(m + of_);
    ups_scratch = (int32_t *)(m + osc);
    ups_st = (ups_st_t *)(m + ost);
    ups_heap_ok = 1u;
    ups_ring_select();
}

static void ups_reconfigure_inner(void)
{
    if (s_probe_on) {          /* engine clean start: await first real feed */
        /* Recording starts ONLY at the first nonzero feed (hook in
         * csd_ups_feed): prefill zeros must not consume the window. */
        s_rec_on = 0u;
        s_probe_reconfpop = s_pop_clock;
        s_probe_reconfigs++;
        s_probe_pop_n = 0u;
        s_probe_feed_n = 0u;
        s_abs_feed = s_abs_calls = 0u;
    }
    const unsigned R = ratio_for(s_in_hz);

    /* Session 17b: pick the effective ring for THIS ratio before any pop can
     * run against it (pop gate, push base and capacity guard all read it). */
    ups_ring_geometry(R);

    ups_state_alloc();
    if (!ups_heap_ok || R > UPS_ALLOC_RATIO) { ups_phases = 0u; return; }
    /* dp127: refuse THIS reconfigure by idling (phases=0 -> inactive),
     * never via s_bypass: the 48k arm-phase asks for R=8 and a sticky
     * bypass here poisoned the later 192k (R=2) arming (hz witnesses:
     * heap allocated + verified fine, bypass stuck at 1). */

    ups_phases = 0u;
    memset(ups_in, 0, 2u * CSD_UPS_FRAME_ADVANCE * 4u);
    memset(ups_q, 0, 2u * CSD_UPS_RING_WORDS * 4u);
    memset(ups_q_cert, 0, 2u * CSD_UPS_RING_CERT_WORDS * 4u);
    /* dp104: reconfigure had missed the compute scratch. With all-zero
     * input blocks the fd compute short-circuits on the ZERO_EXP sentinel
     * and NEVER writes ups_scratch, so the push copied stale scratch words
     * straight into the delivery ring: measured on-device (qpop1 ch0)
     * 2122374088 / -963325592 / 0x80000001 where the clean-state oracle
     * emits zeros. Every other piece of engine state is epoch-cleared;
     * the scratch is state too. */
    memset(ups_scratch, 0, (CSD_UPS_MAX_TD_BLOCK_LENGTH + 8u) * 4u);
    ups_fidx = 0u;
    ups_pushn = 0u;                   /* session 14: double-buffer parity */
    for (unsigned c = 0u; c < 2u; c++) {
        ups_rd[c] = 0u; ups_occ[c] = 0u; ups_last[c] = 0;
    }
    if (R < 2u || R > CSD_UPS_MAX_RATIO) return;

    /* Bank index: the table is ordered {r2, r4, r8} (3 entries) and R is a
     * power of two, so log2(R)-1 == (R >> 2) maps {2,4,8} -> {0,1,2}.
     * The previous expression R/2-1 mapped them to {0,1,3}: ratio 8 read
     * ONE PAST the end of csd_ups_banks[], its num_phases check failed on
     * garbage, ups_phases stayed 0, every csd_ups_feed refused, and the
     * wire emitted host words untouched (zero-order hold). Measured on the
     * 2026-11-10 bench at 48k host: armed=1, ratio=8, blocks=0, pops=0,
     * refusals +233,725 over the stream -- a tone was audible ONLY as the
     * 8x-repeated host word. Ratios 4/2 were unaffected, which is why only
     * the ratio-8 lane never computed. Host-side test_ups_block.py walks
     * the array directly, so it could never see the C-side mis-indexing. */
    const csd_ups_bank_t *bank = &csd_ups_banks[R >> 2u];
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
            memset(ups_blob[p][c], 0, (unsigned)CSD_UPS_MAX_DATA_WORDS * 4u);
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
    ups_reconfigure(0u);   /* REVERT(s15-diag): site = note_stream_rate change */
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
            s_out_hz = 0u; s_bypass = 1u;
            ups_reconfigure(1u); /* REVERT(s15-diag): site = wire reject */
        }
        return;
    }
    if (s_out_hz != out_hz) {
        s_out_hz = out_hz; s_bypass = 0u;
        ups_reconfigure(2u);     /* REVERT(s15-diag): site = wire accept */
    }
}

void csd_ups_set_enabled(unsigned enable)   /* not 'on': reserved in XC */
{
    const unsigned now = enable ? 1u : 0u;
    if (now != s_enabled) {
        s_enabled = now;
        /* The capture tap follows the resampler lifecycle: armed exactly
         * when the client is enabled (csd_ups_slot early-returns unless
         * active, so the tap cannot outlive the path it observes). */
        csd_ups_tap_arm(now);
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
        ups_reconfigure(3u);          /* a mode switch is a stream restart;
                                       * REVERT(s15-diag): site = set_enabled */
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
    return (ups_heap_ok != 0u && s_want && s_enabled && s_out_hz != 0u
            && !s_bypass && ups_phases != 0u)
           ? 1u : 0u;
}

/* Cold-path publisher: recompute the armed-mic gate into the csd_dsp.xc
 * mailbox (UBM hot path reads ONLY that local volatile -- 11.11.7 proved a
 * cross-file call per wire frame blows the 384 kHz budget). Callers: every
 * gate-input mutator (set_want, set_enabled, tap_arm, ups_reconfigure).
 * REVERT with the feed. */
static void csd_ups_push_feed_owner(void)
{
    csd_feed_owner = csd_ups_mic_owner();
}

/* Wrapper so EVERY reconfigure exit (early returns included) republishes. */
static void ups_reconfigure(unsigned site)
{
    /* REVERT(s15-diag): always-on attribution (cold path, ~4 RMWs). */
    s_rc_total++; s_rc_last = site; if (site < 4u) s_rc_site[site]++;
    ups_reconfigure_inner();
    csd_ups_push_feed_owner();
}

unsigned csd_ups_bypass_witness(void)  { return s_bypass; }
unsigned csd_ups_enabled_witness(void) { return s_enabled; }
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
/* EXPERIMENT 11.11.30 stuck-compute witnesses (REVERT with the tap). */
static volatile unsigned s_feed_enter;
static volatile unsigned s_pc;
static volatile unsigned s_in_pc;
static volatile unsigned s_feedin_pub;   /* feed-IN echo publishes (11.11.29) */

void csd_ups_feed(int l, int r)
{
    if (g_csd_probe_mask & 16u) {  /* REVERT(dp201): feed body stage witness, bit4-gated per dp202 lean-gate */
        g_csd_eq_stage = 4u; g_csd_eq_stage_t = csd_ups_now(); }
    { unsigned spm = (unsigned)(uintptr_t)&l; s_witness_sp_feed = spm; } /* dp120 */
    if (s_probe_on && !s_rec_on) { s_abs_calls++; }
    const unsigned R = ups_phases;

    if (!s_enabled || !s_want) return;           /* client off: nothing to do */
    if (s_bypass || s_out_hz == 0u || R == 0u) {
        s_refusals++;                          /* on but refused: count, no queue */
        return;
    }

    /* ---- EXPERIMENT 11.11.29: feed-IN echo (REVERT with the tap) ---------
     * Publish what the engine RECEIVES, not what it pops. Owner-gated so an
     * unarmed stream pays nothing; one host frame = one mic frame, so the
     * capture is the engine input sequence in schedule order, 1:1. Proves
     * (a) the preset-2 EQ chain bit-identity at the feed seam and (b) pins
     * the oracle input exactly. REVERT: delete, restore pop echo below. */
    /* 11.11.36: publish MOVED to csd_dsp.xc audio() entry (mic-clock
     * synchronous). Task-side publish here sampled the engine input at
     * the task's bursty pace and corrupted the mic view. */
    if (csd_ups_mic_owner()) { s_feedin_pub++; }
    /* --------------------------------------------------------------------- */

    /* Absolute feed history (2026-11-12): record EVERY pair while the
     * probe is on, indexed mod RING by an absolute counter. A rec_on-gated
     * window starts at the first nonzero feed and loses the pre-trigger
     * pairs — which are NOT zeros: the 48k drain left real audio in the
     * current fd block (measured el1: fidx=65 of drain data, first mismatch
     * at row 53). The oracle replays the true stream from PROBE arm. */
    if (s_probe_on && !s_rec_on && (l | r)) {
        /* dp101: the trigger runs BEFORE the recorder and the payload
         * index rebases HERE, not at the origin pop. Consequences that
         * the pacing oracle depends on:
         *   - the triggering pair is itself recorded (row 0): earlier the
         *     recorder saw rec_on=0 for it and the oracle's payload table
         *     was shifted by one (fy1: every content row mismatched);
         *   - feeds between the trigger and the origin pop are recorded
         *     and counted (rows 0..N-1, N = s_probe_feedoff). They are
         *     already inside the seeded state, so the oracle SKIPS those
         *     rows when replaying but must still count them when pacing
         *     boundaries (fy1: f-stamp 90 vs boundary at 91 apps). */
        s_probe_pop_n = 0u;
        s_abs_feed = 0u;
        s_abs_calls = 0u;
        s_rec_on = 1u;
        s_probe_pre = s_abs_calls;
        s_probe_fidx = ups_fidx;
        s_feed_abs = 0u;
        s_probe_feedoff = 0u;
        s_snap_wanted = 1u;
    }
    /* dp145 REVERT: atomic-capture quiescence BEFORE counting the
     * payload row. The dp93-era order incremented s_feed_abs and wrote
     * the payload row BEFORE the freeze spin, so a row counted into
     * s_probe_feedoff (read at accept) could still have been spinning
     * when the snapshot copied the window: it never reached ups_in yet
     * the oracle was told to skip it as "already seeded" -- oracle
     * window carried stale seed content in those slots and every
     * post-trigger boundary compute diverged (iq1: 122 seeded replays
     * exact, first computed ring word mismatched). Claim
     * (s_in_feed=1), re-check the freeze, back off on collision; the
     * capture waits for s_in_feed==0, so from here a counted row is a
     * row that WILL be (and then has been) applied into the window. */
    {   /* REVERT(dp201): timed PROBE-bit4 attribution of the claim loop
         * (runs on the tile[1] EQ task when armed; the freeze/backoff
         * spins are the suspects for the feed-claim cost). */
        unsigned t_c = (g_csd_probe_mask & 16u) ? csd_ups_now() : 0u;
        /* REVERT(dp201): task-side freeze witness — stage 2 = claim loop.
         * Unconditional (RMW on task core only); stage_t re-based on every
         * stage entry so a frozen (stage, growing stage_t) pins the wedge. */
        if (g_csd_probe_mask & 16u) {   /* bit4-gated per dp202 lean-gate */
            g_csd_eq_stage = 2u; g_csd_eq_stage_t = csd_ups_now();
            g_csd_eq_claim_rounds++;
            g_csd_live_pop_in_commit = s_pop_in_commit;
            g_csd_live_snap_freeze = s_snap_freeze;
            g_csd_live_in_feed = s_in_feed;
            g_csd_live_probe_popn = s_probe_pop_n; }
        unsigned bospins = 0u;
        for (;;) {
            unsigned spins = 0u;
            /* dp134 TRUTH #14: a stuck freeze must never kill the audio
             * path -- same bound as the capture spin. */
            while (s_snap_freeze && spins++ < 20000000u) { }
            s_in_feed = 1u;                          /* claim */
            /* dp148 REVERT: pair-atomic claim. A pop PAIR commits across
             * two csd_ups_slot calls (ch0 then ch1); claiming mid-pair
             * lets ch0 read the old ring, the boundary push replace it,
             * and ch1 (dp147 halt) read the new ring -- one wire pair
             * straddling the boundary (jc1: pop row 246). s_pop_in_commit
             * (dp118) marks the pair critical section; back off and
             * claim only at pair boundaries. */
            if (!s_snap_freeze && !s_pop_in_commit) break;
            s_in_feed = 0u;                          /* back off, retry */
            if (++bospins > 65536u) break;           /* bench-safety valve
                                                      * (dp151: a claim may
                                                      * now legitimately
                                                      * wait out one pair,
                                                      * ~1 LRCLK frame) */
        }
        /* REVERT(dp201): close the claim-attribution window. */
        if (g_csd_probe_mask & 16u) { g_csd_eq_claim_exit++; }  /* REVERT(dp201) */
        if (g_csd_probe_mask & 16u) { unsigned d = csd_ups_now() - t_c;
            g_csd_p_claim += d; if (d > 64u) g_csd_p_claim_n++; }
    }
    if (s_rec_on && s_feed_abs < CSD_PROBE_FRING) {
        /* Freeze-on-full from PROBE arm (mirrors the feed-EVENT ring, which
         * froze at 8192): payload rows and event rows must cover the SAME
         * absolute sequences. Overwriting old payloads while events still
         * referenced them produced 4400 unresolvable feed events (en1). */
        const unsigned a = s_feed_abs;
        s_probe_feed[a][0] = l;
        s_probe_feed[a][1] = r;
        s_feed_abs = a + 1u;                          /* single writer */
        if (s_probe_fev_tot < CSD_PROBE_EV) {
            const unsigned e = s_probe_fev_tot;
            s_probe_fev_t[e] = s_pop_clock & 0x7FFFFFFFu;   /* stamp only */
            s_probe_fev_tot = e + 1u;
        }
    }
    if (s_rec_on) { s_abs_feed++; }
    if (s_arm_seen != s_arm_gen) {       /* first armed frame of a new arm */
        s_arm_seen = s_arm_gen;
        ups_fidx = 0u;
        ups_pushn = 0u;                  /* session 14: region parity + lead
                                          * restart with the rings */
        for (unsigned c = 0u; c < 2u; c++) { ups_rd[c] = 0u; ups_occ[c] = 0u; }
    }
    ups_in[0][ups_fidx] = (int32_t)l;
    ups_in[1][ups_fidx] = (int32_t)r;
    s_probe_wchk = (s_probe_wchk ^ (unsigned)l) + 0x9E3779B9u + (unsigned)r;
    if (++ups_fidx < CSD_UPS_FRAME_ADVANCE) { s_in_feed = 0u; return; }
    ups_fidx = 0u;

    /* Block boundary: run the whole polyphase bank. This is the compute
     * burst -- amortised over frame_advance * R wire frames (1024 slots at
     * ratio 8), NOT per slot. The witness counters below exist so the bench
     * can judge it against the LRCLK budget over DIAG:UPSAMPLing:INFO?. */
    const unsigned t0 = csd_ups_now();
    /* REVERT(dp201): stage 5 = block FIR compute (the 11.12.6 wedge site),
     * bit4-gated per dp202 lean-gate. */
    if (g_csd_probe_mask & 16u) { g_csd_eq_stage = 5u; g_csd_eq_stage_t = t0; }
    /* Same index rule as ups_reconfigure (see the long comment there):
     * R>>2 maps {2,4,8} -> {0,1,2}. It MUST match -- if feed indexed a
     * different bank than the one that was validated and loaded, the
     * compute would run on the wrong coefficient set. */
    const csd_ups_bank_t *bank = &csd_ups_banks[R >> 2u];
    /* EXPERIMENT 11.11.30: stuck-compute witness (REVERT with the tap).
     * The EQ task froze once armed+pushed; s_pc names the (phase,channel)
     * compute entered last and s_in_pc stays 1 while one is running.
     * INFO fields feed_pc / feed_in_pc / feed_core during a freeze name the
     * stuck call; feed_enter counts block entries per caller (see also
     * s_blocks). */
    s_feed_enter++;
    const unsigned shift = s_ratio_shift;
    const unsigned fill  = CSD_UPS_FRAME_ADVANCE * ups_phases;
    /* Session 16 root cause (fix96, 2026-11-14): the delivery ring is
     * drained LINEARLY by the pop (rd++ & MASK), so the push cursor must be
     * the matching FIFO position, not a two-region parity. With the
     * session-14 parity base (pushn&1)*fill the pop could only ever trail
     * the commit by <= one block (it reads region k-1's words at linear
     * position (k-1)*fill mod RING, which coincides with the parity region
     * only while the pop stays close behind); the measured pre-commit occ
     * sat at ~98 words -- a JIT stream. Any late commit (529 us worst
     * compute vs a 1.33 ms block period at ratio 4) drained the ring and
     * the pop rode ring-empty, holding ups_last (the LAST-PUSHED word: a
     * future-block sample, a sine near-zero ~ as often as not) for 200-256
     * pops: the deterministic 256-zero-runs-every-512 host samples,
     * zerofrac 0.39-0.43, harm -15..-20 and ~10-16% reps at ratio 4, with
     * the pop's own words proven non-zero (s16 popz witnesses: 0/0 under
     * bit6). At ratio 2 the double period and half compute hide it (fixT
     * CLEAN). FIFO base = popn + occ + lead: the ring becomes its full
     * 2048/fill blocks (4 at ratio 4) of slack; the capacity guard and the
     * pop lead gate keep the cursors legal (pop never overtakes, push never
     * writes within occ words of rd). At ratio 8, fill == RING/2 and this
     * is exactly the old parity map -- shipping behaviour unchanged there. */
    const unsigned base  = (ups_pushn * fill) & ups_ring_mask;
    /* Double-buffer capacity guard (session 14): the new block lands in
     * the region the pop stream consumed TWO blocks ago; it may be
     * overwritten only once at least one block (fill words) has been
     * consumed from the ring. Steady state never spins here (fill rate ==
     * drain rate); the bound + s_push_over witness keep the failure
     * honest if that ever stops holding. occ only shrinks on the pop
     * side, so checking before the compute burst is sound: nothing here
     * grows occ until the end-of-block commit. */
    {   /* Session 16 close-out (fixT crawl control + attribution): this wait
         * used to be a 2M-iteration bounded spin on occ+fill > RING. Two
         * defects: (1) the spin forms a cross-core wait cycle -- the task
         * waits HERE on the producer's pop to drain occ, while the
         * producer's pass blocks in csd_ub_consume_if_new ->
         * UserBufferManagement -> csd_eq_handoff's armed flow-control spin
         * waiting on THIS task to drain the EQ in-ring; once drift puts occ
         * over the line both waits go active and the system locks into the
         * timeout-crawling equilibrium measured on-device at 192k armed
         * (blocks ~3.5% of rate, UBM ~25 us/call ~= whole pass time,
         * eq_stalls/eq_overrun ~1k/s). The spin can never fix the drift
         * (the pop lives on the thread stuck behind this task), so NEVER
         * wait: drop the block (honest wire repeat, self-recovering).
         * (2) RING was not a lead bound: at ratio 2 the task pre-ran seven
         * blocks ahead and steady occ pinned AT the guard -- every commit
         * raced the pop (drop/pushov ~40/s, R-window repeats ~29k/s,
         * 3 x 256-word deficits per window). The real contract is the
         * double-buffer LEAD, so cap the task at seed+1 committed blocks:
         * steady occ sits between seed and seed+1 blocks of absorption.
         * At ratio 4 (seed 3) seed+1 blocks IS the ring, and at ratio 8 the
         * ring is the tighter bound, so those ratios are bit-identical to
         * the shipped gates; only ratio 2's cap tightens (768 << 2048).
         * pushov stays as the anomaly witness: a balanced stream never
         * takes this branch. */
        const unsigned cap = (ups_lead_seed() + 1u) * fill;
        const unsigned capx = (cap < ups_ring_w) ? cap : ups_ring_w;
        if (ups_occ[0] + fill > capx || ups_occ[1] + fill > capx) {
            ups_pushov++;
            s_in_feed = 0u;
            return;
        }
    }
    for (unsigned c = 0u; c < 2u; c++) {
        int32_t *q = ups_qp[c];
        for (unsigned p = 0u; p < R; p++) {
            s_pc = (p << 1) | c;
            s_in_pc = 1u;
            if (c == 0u && p == 0u && s_probe_headc == 999999u) {
                s_probe_headc = (unsigned)ups_data[0][0].head_index;
                s_probe_datac = (unsigned)((int32_t *)ups_data[0][0].data_blocks[0].data)[0];
            }
            /* dp144 REVERT: post-accept boundary-entry latch (see the
             * s_probe_bnd_* definitions). Gated on s_probe_feedoff so it
             * arms exactly at capture accept on the device AND in the
             * xsim oracle (the harness sets s_probe_feedoff = feedoff),
             * sharing this one code path. */
            if (c == 0u && p == 0u && s_probe_feedoff
                && s_probe_bnd_done < 2u) {
                const unsigned bk = s_probe_bnd_done;
                unsigned h0 = 2166136261u, h1 = 2166136261u;
                s_probe_bnd_feed[bk] = s_feed_abs;
                s_probe_bnd_pop[bk] = s_pop_clock;
                for (unsigned i = 0u; i < (unsigned)CSD_UPS_FRAME_ADVANCE; i++) {
                    h0 = (h0 ^ (unsigned)ups_in[0][i]) * 16777619u;
                    h1 = (h1 ^ (unsigned)ups_in[1][i]) * 16777619u;
                }
                s_probe_bnd_wc0[bk] = h0;
                s_probe_bnd_wc1[bk] = h1;
                s_probe_bnd_head[bk] = (unsigned)ups_data[0][0].head_index;
                s_probe_bnd_fa[bk] = bank->phases[0].frame_overlap;
            }
            fd_block_fir_add_data(ups_in[c], &ups_data[p][c]);
            fd_block_fir_compute(ups_scratch, &ups_data[p][c], &ups_filter[p]);
            s_in_pc = 0u;
            /* dp144 REVERT: compute-output latch, same boundary gate. */
            if (c == 0u && p == 0u && s_probe_feedoff
                && s_probe_bnd_done < 2u) {
                const unsigned bk = s_probe_bnd_done;
                const int32_t *sv = &ups_scratch[s_probe_bnd_fa[bk]];
                s_probe_bnd_s0[bk] = (unsigned)sv[0];
                s_probe_bnd_s1[bk] = (unsigned)sv[1];
                s_probe_bnd_s2[bk] = (unsigned)sv[2];
                s_probe_bnd_s3[bk] = (unsigned)sv[3];
                s_probe_bnd_s15[bk] = (unsigned)sv[15];
                /* dp146 REVERT: push-window bisection crcs + block crc. */
                {   unsigned blk;
                    for (blk = 0u; blk < 4u; blk++) {
                        unsigned cc = 2166136261u;
                        const unsigned bi = s_probe_bnd_fa[bk] + blk * 32u;
                        for (unsigned i = 0u; i < 32u; i++)
                            cc = (cc ^ (unsigned)ups_scratch[bi + i]) * 16777619u;
                        s_probe_bnd_scrc[bk][blk] = cc;
                    }
                    {   unsigned cc = 2166136261u;
                        const int32_t *bd0 = (const int32_t *)
                            ups_data[0][0].data_blocks[0].data;
                        for (unsigned i = 0u; i < 256u; i++)
                            cc = (cc ^ (unsigned)bd0[i]) * 16777619u;
                        s_probe_bnd_bcrc[bk] = cc;
                    }
                    if (bk == 0u) {
                        s_probe_bnd_pblob = (unsigned)(uintptr_t)ups_blob[0][0];
                        s_probe_bnd_pdata = (unsigned)(uintptr_t)
                            ups_data[0][0].data_blocks[0].data;
                        s_probe_bnd_pscratch = (unsigned)(uintptr_t)ups_scratch;
                    }
                }
                s_probe_bnd_done = bk + 1u;
            }
            /* The valid frame is [frame_overlap, frame_overlap +
             * frame_advance) of the compute output -- the same window
             * csd_dsp_fd.c uses; de-interleaved to wire order: input step j,
             * phase p -> wire word (j << shift) + p. */
            const int32_t *v = &ups_scratch[bank->phases[p].frame_overlap];
            for (unsigned j = 0u; j < CSD_UPS_FRAME_ADVANCE; j++) {
                q[(base + ((j << shift) + p)) & ups_ring_mask] = v[j];
                if (c == 0u && p == 0u && s_probe_qw_n < 64u) {
                    s_probe_qwpop[s_probe_qw_n] = s_pop_clock; s_probe_qw_n++;
                }
            }
        }
        if (c == 0u) {
            s_feed_qaddr = (unsigned)(uintptr_t)ups_qp[0];
            s_feed_tile  = (unsigned)get_local_tile_id();
        }
        if (c == 0u) s_probe_c0pop = s_pop_clock;  /* stagger witness */
    }
    /* Session 14: SINGLE end-of-block commit. dp195 occ honesty still
     * governs -- occ advances by the extent WRITTEN this block (the far
     * region [base, base+fill)), never the ring allocation. The commit was
     * hoisted out of the c loop because the producer pops a PAIR (L then
     * R) per pass: committing per channel let a pass consume block k+1's
     * L while occ_R had not advanced yet (R still computing), so every
     * pass inside the c1 window manufactured an R repeat -- measured
     * repsR 38.2k/s (10% of pops) with repsL 0 at ratio 4, and the same
     * lottery flipped to repsL 65.7k on the ECHO-1 leg (phase, not
     * channel, is the culprit). occ[c] += fill keeps the accumulated
     * two-block lead across the compute burst. */
    for (unsigned c = 0u; c < 2u; c++) {
        /* REVERT(s16-diag): commit-time view + stale-overwrite classification
         * (target region first word still non-zero while occ says consumed =>
         * the pop stream is behind the parity region map). */
        if (g_csd_probe_mask & 64u) {
            s16_pbase = base;
            s16_pocc[c] = ups_occ[c] + fill;
            if (ups_qp[c][base & ups_ring_mask] != 0) s16_ovlv++;
        }
        ups_occ[c] += fill;
        /* s16: ups_last now maintained by the pop side (honest last-POPPED
         * word); the old commit-time assignment published a future-block
         * word as the starvation repeat — see the pop-site comment. */
        if (s_occ_high[c] < ups_occ[c])
            s_occ_high[c] = ups_occ[c];
    }
    ups_pushn++;
    const unsigned dt = csd_ups_now() - t0;
    if (g_csd_probe_mask & 16u) { g_csd_eq_stage = 4u; } /* REVERT(dp201):
     * compute done, back in feed body; bit4-gated per dp202 lean-gate */
    if (dt > s_block_ticks_max) s_block_ticks_max = dt;
    if (s_probe_blk_n < 8u) {          /* first-boundary pop clocks (dp99) */
        s_probe_blkpop[s_probe_blk_n] = s_pop_clock;
        s_probe_blk_n++;
    }
    s_blocks++;
    s_frames++;
    s_in_feed = 0u;
}

/* ---- wire-word capture tap state (bench instrumentation) ---------------
 * Writers: audiohub core only, inside csd_ups_slot. Readers: same core
 * (csd_dsp.xc UBM mirror) and the diagnostic thread (witnesses only).
 * s_tap is published by csd_ups_tap_arm from the SCPI thread -- volatile
 * single-word publication, same pattern as s_want. REVERT with the tap. */
static volatile unsigned s_tap = 0u;            /* DEBUG:LOOPback gate      */
static unsigned          s_tap_slot = 0xFFFFFFFFu; /* AUTO -> R-1           */
static int32_t           s_tap_pend[2];         /* pending half-pair        */
static volatile int32_t  s_tap_lat[2];          /* committed pair (reader)  */
static volatile unsigned s_tap_fires, s_tap_last_phase;
/* Pair-atomicity seqlock (2026-11-11): the mixer core reads the committed
 * pair from mixer1's GiveSamplesToHost (armed mic feed). audio() and mixer1
 * are separate threads; the two word stores below are not one transaction,
 * so a reader that straddles a commit could pair frame k's L with frame
 * k+1's R -- content errors indistinguishable from a device fault. Bump
 * around the commit; readers retry. Cost: two stores per fire, two loads
 * per read. REVERT with the tap. */
static volatile unsigned s_tap_seq;

void     csd_ups_tap_arm(unsigned enable)  { s_tap = enable ? 1u : 0u; csd_ups_push_feed_owner(); }
unsigned csd_ups_tap_armed(void)           { return s_tap; }
void     csd_ups_tap_set_slot(unsigned p)  { s_tap_slot = p; }
int      csd_ups_tap_word(unsigned ch)     { return (int)s_tap_lat[ch & 1u]; }

/* 2026-11-12: seq-stable pair read for the ECHO-0 mirror. The pop mirror
 * saw [x,0] forever (R word always zero) although the tap commits both
 * words — rather than keep debugging the intermediate csd_pop_w0/1 copy,
 * the mirror now reads the AUTHORITATIVE latch directly, seqlocked: an
 * odd/even race around a commit yields stale==fresh check failure and
 * the caller publishes a zero pair (dead frame, scorer-safe).
 * REVERT with the mirror knobs. */
int csd_ups_tap_pair(int out[2], unsigned *seq)
{
    unsigned s0 = s_tap_seq;
    if (s0 & 1u) return 0;
    out[0] = s_tap_lat[0];
    out[1] = s_tap_lat[1];
    if (s_tap_seq != s0) return 0;
    *seq = s0;
    return 1;
}
unsigned csd_ups_diag_tap_fires(void)      { return s_tap_fires; }
unsigned csd_ups_diag_tap_phase(void)      { return s_tap_last_phase; }

/* Armed mic feed (2026-11-11, F-B): while the resampler owns the wire, the
 * USB mic payload is owned by mixer1's GiveSamplesToHost (proven 2026-11-11
 * run: tap firing 1:1 with UBM, phase == slot, capture still uninitialized
 * RAM -- the UBM mirror's words never reach the endpoint armed). The mixer
 * asks three SCALAR questions per frame -- a pointer parameter across the
 * XC/C boundary does not link in this tree (measured: "Type of symbol
 * ... has mismatch with previous definition" at link), so no out-param:
 *   csd_ups_mic_owner()      -- 1 iff the tap owns the mic slots this frame
 *   csd_ups_diag_tap_seq()   -- seqlock sampler (pair snapshot validator)
 *   csd_ups_tap_word(ch)     -- the committed wire word for that channel
 * The mixer takes ONE pair snapshot per frame (both words + seq check,
 * retry once) and picks L/R from that snapshot per channel, so the pair it
 * publishes cannot straddle two fires. Owner gate is one load + one call:
 * hot-path rule (no mul/div/mod) holds at wire cadence. REVERT with the tap. */
unsigned csd_ups_mic_owner(void)
{
    /* csd_ups_active() is EXACTLY the condition under which csd_ups_slot
     * pops and the tap commits a pair -- so the gate cannot open while
     * s_tap_lat still holds its static 0 (the run5 ratio-1 zero-clamp:
     * tap-arm follows enable at csd_ups_set_enabled, so 'enabled' is true
     * on bypassed ratio-1 legs and the old gate opened on nothing).
     * REVERT with the tap. */
    return (s_tap && csd_ups_active()) ? 1u : 0u;
}

unsigned csd_ups_diag_tap_seq(void) { return s_tap_seq; }

unsigned csd_ups_diag_tap_slot_eff(void)
{
    return (s_tap_slot >= ups_phases) ? (ups_phases - 1u) : s_tap_slot;
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
    return csd_ups_pop_active(ch, w);
}
/* REVERT(dp198): checked body split so the producer (which holds its own
 * armed gate every pass and refreshes g_csd_ps_active /16) can skip the
 * cross-TU csd_ups_active() call on both pops (~15 t/pass). If active is
 * transiently off, this body pops an unprepared ring: occ==0 gives the
 * honest last-repeat pair, which the wire ignores (it reads the mailbox
 * only while g_csd_ps_active). */
int csd_ups_pop_active(unsigned ch, int w)
{
    /* Session 14: seed pop-side LEAD at arm/reconfigure.
     * The wire pops from the instant the client arms, but the engine
     * needs a block period (frame_advance host pairs) to produce the
     * first block and another to have two committed; consuming inside
     * that window rode whatever lead the transient left -- at ratio 4+
     * the transient left none and the pop stream rode ring-empty
     * (JIT delivery), every c1 commit landing after its pops. Hold the
     * last pair (zeros after the epoch-clear; NO rep credit: this is
     * fill phase, not starvation) until the lead commits land, so the
     * pop always drains old blocks while the next computes.
     * Session 16 (fix96): two blocks was a zero-margin seed. Pops drain
     * exactly one block per commit period, so with a 2-block seed the
     * occupancy sits AT zero when each commit lands: any commit jitter
     * (worst compute 529 us vs a 1.33 ms ratio-4 block period) drained
     * the ring and the pop rode the repeat branch ~14% of pops (repsL
     * 55k/s, phase lottery -- a window with holds fails the peak rule,
     * a lucky window passes). Seed THREE blocks whenever the ring fits
     * them (RING/fill >= 3: true at ratio<=4, RING=4x fill there):
     * steady occupancy 2 blocks, one full block of absorption for
     * commit jitter, and the FIFO write cursor stays one block clear of
     * the read cursor. At ratio 8, fill == RING/2 and three blocks do
     * not fit (the capacity guard would wedge commit 2 with pops held),
     * so the seed stays 2 -- the maximum legal there, behaviour as
     * shipped. csd_ups_pop_active has no other pre-commit caller. */
    {   /* Session 16 close-out (fixT control experiment): seed 3 only where
         * one block is >= RING/4 -- the zero-margin (JIT) regime that needs
         * the extra absorption block (ratio 4). At ratio 2 the third seed
         * block holds the pop stream an extra block period at arm/re-seed;
         * the fixT legs on the seed-3 image crawled (blocks ~13% of rate,
         * producer capacity-guard spins push_over > 0, peak 955-957), the
         * seed-2 control did not. Seed 2 at ratio 2 and 8. The feed-side
         * commit cap shares ups_lead_seed() -- see the capacity guard. */
        /* Session 16 close-out: pushn/seed are control-plane values (task
         * writes them per commit, ~1.5k/s); sample every 8th pass. A re-arm
         * can leave the gate open for <= 8 passes (~2 us) -- bounded and
         * pre-fill phase, same credit as the boot hold. */
        static unsigned pn_c = 0u, sd_c = 2u, pn_div = 0u;
        if (++pn_div >= 8u) { pn_div = 0u; pn_c = ups_pushn; sd_c = ups_lead_seed(); }
        if (pn_c < sd_c) {
            /* REVERT(s16-diag): lead-gate hold counter — words emitted through
             * the pre-commit return are invisible to the pop witnesses; this
             * closes that blind spot (would show 2-block zero windows per
             * pushn re-seed). */
            if (s_pass_pm & 64u) {
                s16_lgg[ch]++;
                if ((int)s_ups_last[ch & 1u] == 0) s16_lggz[ch]++;
            }
            return (int)s_ups_last[ch & 1u];
        }
    }
    /* dp118: the task core owns the capture; wait out its halt window.
     * LRCLK is software-generated in this loop, so the delay is benign. */
    {   /* REVERT(dp201): timed PROBE-bit4 attribution (see witness decls). */
        unsigned t_h = (s_pass_pm & 16u) ? csd_ups_now() : 0u;
        unsigned hw = 0u; while (s_pop_halt && hw++ < 20000000u) { }
        if (s_pass_pm & 16u) { unsigned d = csd_ups_now() - t_h;
            g_csd_p_halt += d; if (d > 64u) g_csd_p_halt_n++; } }
    /* dp147 REVERT: ATOMIC FEED -- pops commit only while no feed claim
     * is held, so the delivery ring is never consumed mid-boundary (no
     * torn push window, no c0/c1 stagger to model: the engine's output
     * becomes a pure function of the feed/pop event sequence, which the
     * xsim oracle replays identically). LRCLK is software-generated and
     * the stall is bounded (same bound as dp118/dp134); gated on
     * s_rec_on so shipping behaviour (probe off) is untouched. */
    if (s_rec_on) {
        /* REVERT(dp201): timed PROBE-bit4 attribution. */
        unsigned t_f = (s_pass_pm & 16u) ? csd_ups_now() : 0u;
        unsigned fw = 0u; while (s_in_feed && fw++ < 20000000u) { }
        if (s_pass_pm & 16u) { unsigned d = csd_ups_now() - t_f;
            g_csd_p_ifed += d; if (d > 64u) g_csd_p_ifed_n++; } }
    /* REVERT(dp198): dp120 s_witness_sp_slot store removed from every pop
     * (~3 t); the DP-view bug class it hunted closed at dp131. */
    int32_t v;
    unsigned idx_pop = 0xFFFFFFFFu;    /* dp95: ring word consumed */
    unsigned kabs = 0u;                  /* dp128: fn-scope (accept block needs it) */
    /* Wire phase BEFORE any bump: pop index mod R is the polyphase phase
     * (the de-interleave pushes (j<<k)+p); both rings reset together per
     * block push, so the phase is globally consistent. During starvation
     * rd legitimately stalls (honest repeat) -- the witnesses tell. */
    s_pop_in_commit = 1u;              /* dp118: capture mutual exclusion */
    const unsigned phase = ups_rd[ch] & (ups_phases - 1u);
    if (ups_occ[ch] == 0u) {
        s_repeats[ch]++;               /* starvation, visible on the host */
        v = s_ups_last[ch];
    } else {
        idx_pop = ups_rd[ch];
        v = ups_qp[ch][idx_pop];
        ups_rd[ch] = (ups_rd[ch] + 1u) & ups_ring_mask;
        ups_occ[ch]--;
        s_pops[ch]++;
        /* Session 16: hold-last is the last POPPED live word, not the last
         * pushed word. The commit-time assignment made a starvation repeat
         * emit a FUTURE-block sample (a content lie); repeating the last
         * emitted word is the honest wire repeat. Boot/pre-pop value stays
         * 0 (lead-gate fill phase, no rep credit — unchanged). Producer
         * static (see s_ups_last decl): writer and readers are this core. */
        s_ups_last[ch] = v;
    }
    /* REVERT(s16-diag): zero-value pop bucketing, PROBE bit5 only. One mask
     * load; counts BOTH the honest-repeat zeros and real pops of zero words
     * (the host cannot tell them apart; reps counters attribute). */
    if (s_pass_pm & 64u) {
        if (v == 0) { s16_popz[ch]++; s16_blkcnt[ch]++; }
        if (++s16_blkph[ch] >= CSD_UPS_FRAME_ADVANCE * ups_phases) {
            s16_blkph[ch] = 0u;
            s16_blkz[ch][3] = s16_blkz[ch][2]; s16_blkz[ch][2] = s16_blkz[ch][1];
            s16_blkz[ch][1] = s16_blkz[ch][0]; s16_blkz[ch][0] = s16_blkcnt[ch];
            s16_blkcnt[ch] = 0u;
        }
    }
    /* ---- wire-word capture tap (bench instrumentation) -----------------
     * ONE writer: the audiohub core, here only. Reader: the UBM mirror in
     * csd_dsp.xc, same core, later in the frame iteration. The tap
     * OBSERVES pops; it advances nothing. Fires once per LRCLK pair: the
     * R-channel pop closing the pair, at ring phase == slot (AUTO = R-1,
     * the mic-decimator rule). The honest-repeat word latches too -- the
     * tap mirrors the wire, warts and all. Off unless armed via
     * csd_ups_tap_arm: cost disarmed is one load + branch per pop.
     * REVERT: delete this block and the tap API. */
    /* 2026-11-12: pop-pair publication moved OUT of the s_tap gate.
     * Why: the ECHO=0 mic mirror (csd_dsp.xc) reads csd_pop_w0/1 + seqlock,
     * but the pop pass never armed the bench tap (tap arm rides the client
     * enable yet tap_fires stayed 0 armed — an unresolved tap-gate defect),
     * so the pop echo published boot zeros: a whole capture of nothing.
     * The mailbox stores are four writes once per host frame — bounded,
     * same-core, seqlocked; they mirror the wire unconditionally now. The
     * tap's own counters/latency latch stay gated below.
     * REVERT: fold these stores back inside the s_tap block. */
    s_tap_pend[ch] = v;
    if (ch == 1u) {
        csd_pop_seq++;
        csd_pop_w0 = (int)s_tap_pend[0];
        csd_pop_w1 = (int)v;
        csd_pop_seq++;
        /* Pop clock counts EVERY committed pop pair since the last
         * reconfigure — prefill repeats included (measured ek1: the trigger
         * sits ~79 pops after the rate change, and a rec_on-gated clock
         * reported prepop=0 and collapsed the two stamp time bases).
         * One writer (audio core); the task only reads it for feed stamps.
         * REVERT with the rings. */
        if (s_pop_clock_reset) { s_pop_clock = 0u; s_pop_clock_reset = 0u; }
        kabs = s_pop_clock;
        /* dp114: csd_ups_slot is also invoked on PASS-THROUGH calls from
         * ANOTHER core (idx_pop == 0xFFFFFFFF). That core's DP window
         * holds ZERO copies of ups_q/ups_blob (dp113: direct ring read
         * = 0 on the snapshot core while the pop path on the engine core
         * reads real words — same function, different caller). The
         * pass-through caller was consuming s_snap_wanted, so every
         * capture came back body-empty. Capture only on a REAL pop. */
        if (s_snap_wanted && idx_pop != 0xFFFFFFFFu && kabs >= 256u) {
            /* dp118: claim only — the TASK core performs the capture at
             * the end of feed() with the pops halted. kabs>=256 keeps the
             * origin out of prefill (dp116 lesson). */
            s_snap_wanted = 0u;
            s_snap_kabs = kabs;
            /* dp149 REVERT: base the per-channel qpop1 row maps HERE, at
             * the claiming ch1 commit on this core — the snapshot/reset
             * block runs on core A milliseconds later, and this core's
             * next commits must already map through the new base. The
             * accepting pair counts on both channels (ch0 consumed itself
             * at entry; this commit counts now). */
            /* dp150: s_qpop1_base[0] is captured on core A inside the
             * accept reset (ch0 is popped on core A; the base must be an
             * A-local read of an A-written counter). */
            s_diag_qb[0] = s_qpop1_base[0];
            s_diag_qb[1] = s_qpop1_base[1];
            s_diag_pops_claim[0] = s_pops[0];
            s_diag_pops_claim[1] = s_pops[1];
            s_diag_popn_claim = s_probe_pop_n;
            s_diag_kabs_claim = kabs;
            s_snap_claimed = 1u;
        }
        if (s_snap_wanted && idx_pop != 0xFFFFFFFFu && kabs < 256u)
            s_snap_noowner++;
        s_pop_clock = kabs + 1u;
        if (s_rec_on) {
            const unsigned k = s_probe_pop_n;
            if (k < CSD_PROBE_RING) {
                s_probe_pop[k][0] = s_tap_pend[0];
                s_probe_pop[k][1] = v;
                s_probe_pop_n = k + 1u;
            }
            if (s_probe_pev_tot < CSD_PROBE_RING) {
                const unsigned e = s_probe_pev_tot;
                s_probe_pev_t[e] = s_feed_abs & 0x7FFFFFFFu;
                /* CAUSAL pair (2026-11-12): the pop's own seq is its ring
                 * index; what the oracle needs is the feed counter at the
                 * pop instant. Cross-core timestamps (dp82: rows 0..82
                 * exact then interleave jitter) are replaced by this
                 * happens-before pair: pop k lands after feed f-1. */
                s_probe_pev_tot = e + 1u;  /* single writer                  */
            }
        }
    }
    if (s_tap) {
        if (ch == 1u && phase == (s_tap_slot & (ups_phases - 1u))) {
            s_tap_seq++;                 /* seqlock: pair mid-commit visible */
            s_tap_lat[0] = s_tap_pend[0];
            s_tap_lat[1] = s_tap_pend[1];
            /* Publish to the armed-mic mailbox (csd_dsp.xc UBM reads it;
             * SAME core, later in this audio() iteration -- ordering is
             * sequential, no fence needed). REVERT with the feed. */
            /* EXPERIMENT 11.11.34 DUAL ECHO: pop words publish to their
             * OWN mailbox now (csd_pop_w0/1); the mirror alternates feed
             * and pop frames. REVERT with the tap. */
            csd_pop_seq++;                 /* odd: mid-commit */
            csd_pop_w0 = (int)s_tap_lat[0];
            csd_pop_w1 = (int)s_tap_lat[1];
            csd_pop_seq++;                 /* even: pair complete */
            s_tap_last_phase = phase;
            s_tap_fires++;
            s_tap_seq++;
        }
    }
    if (s_rec_on && idx_pop != 0xFFFFFFFFu) {
        /* dp95 decisive witness: re-read the consumed ring word AFTER
         * commit. Equal: the pop really delivered the ring. Different:
         * SOMETHING rewrote that entry in the same commit window — and
         * for ch1 we also ship the post-commit ring word to the host. */
        const int32_t rw = ups_qp[ch][idx_pop];
        if ((int)rw != v) { if (ch == 0u) s_probe_mis0++; else s_probe_mis1++; }
        if (ch == 0u) {
            /* dp149 REVERT: ch0 rows by core A's OWN consumption counter,
             * based at the accept reset — never by core B's pair frontier
             * (whose lead re-skewed the whole array at the first boundary,
             * jd1). Rows 1..RING; pre-reset pairs see the previous capture
             * base and are rejected, re-covered 1:1 by post-reset pairs. */
            const unsigned prow = s_pops[0] - s_qpop1_base[0];
            if (prow >= 1u && prow <= CSD_PROBE_RING)
                s_probe_qpop1_buf[0][prow - 1u] = (unsigned)rw;
        } else if (s_probe_pop_n &&
                   (s_probe_pop_n - 1u) < CSD_PROBE_RING) {
            /* dp150: same accept-reset row as the pev ring (both read by
             * this core in this commit path) — pair-aligned with ch0;
             * dp150 adds the missing bounds guard. */
            s_probe_qpop1_buf[1][s_probe_pop_n - 1u] = (unsigned)rw;
        }
        /* dp120 REVERT: capture here — this block's single-index read of
         * ups_q demonstrably sees real words (qpop1). Run the full state
         * copy from this exact codegen context, on a pop that delivered a
         * nonzero word (proves this core's view is the live one). Feed is
         * frozen for the duration (dp93 handshake restored). */
        /* dp131 REVERT: pin the capture to the ch0 pop core. csd_ups_slot
         * runs on TWO cores (one per channel); with claimed set on both,
         * the two retake loops trample each other's arena writes and the
         * freeze handshake (id1: entry=5873, zero accepts, progress dies
         * at word 2 — interleaved passes). One core, one copier. */
        if (s_snap_claimed && ch == 0u && (int)rw != 0) {
            /* REVERT(dp201): timed PROBE-bit4 attribution (whole in-pop
             * capture, entry through freeze release). */
            unsigned t_s = (g_csd_probe_mask & 16u) ? csd_ups_now() : 0u;
            unsigned spm2 = (unsigned)(uintptr_t)&rw;
            s_witness_sp_snap = spm2;
            s_snap_freeze = 1u;
            { unsigned sp = 0u; while (s_in_feed && sp++ < 20000000u) { } }
            unsigned tries = 0u, ok = 0u;
            unsigned rd0 = 0u, oc0 = 0u, rd1 = 0u, oc1 = 0u;  /* dp137 hoist */
            if (s_snap_groups >= 64u) { s_snap_claimed = 0u; s_snap_groups = 0u;
                                        s_snap_noowner += 1u; }  /* TRUTH #8 */
            do {
                const unsigned rc0 = s_probe_reconfigs, ag0 = s_arm_gen;
                csd_probe_snapshot();
#define CSD_SNAP_HDR (2 + 7 + CSD_SNAP_RINGW + 8 * CSD_SNAP_PER_BANK)
                /* dp132 REVERT: the dp93-era cursor pairing (word 259 vs
                 * 261) is occ0 vs rd1 in the current layout — an
                 * impossible pairing for a REAL state (|1005-0|>64), so
                 * only half-zero snapshots ever passed. Validate the true
                 * per-channel invariant rd<=occ<=RINGW instead. */
                /* dp138 FIELD MAP from the WRITER (csd_probe_snapshot
                 * 1100-1115): 0=R 1=fidx 2..257=window 258=rd0 259=occ0
                 * 260=last0 261=rd1 262=occ1 263=last1. dp93 indices were
                 * right; gen2's DEVICECHK labels (rd1@260, occ1@261) were
                 * the stale ones. */
                rd0 = csd_probe_snap_word(258u);
                oc0 = csd_probe_snap_word(259u);
                rd1 = csd_probe_snap_word(261u);
                oc1 = csd_probe_snap_word(262u);
                s_wit_g[0] = rd0; s_wit_g[1] = oc0;      /* dp135: what the
                s_wit_g[2] = rd1; s_wit_g[3] = oc1;      /* gate really saw */
                if (s_probe_reconfigs == rc0 && s_arm_gen == ag0
                    && rd0 <= oc0 && rd1 <= oc1
                    && oc0 <= CSD_UPS_RING_WORDS && oc1 <= CSD_UPS_RING_WORDS
                    /* dp134: push fills both channels together; a torn
                     * mid-push view is grossly asymmetric (1024 vs 0). */
                    && ((oc0 > oc1) ? (oc0 - oc1) : (oc1 - oc0)) <= 2u
                    /* dp136: symmetry alone lets a never-pushed (empty,
                     * consistent) ring through (ii1 gate: 0/1024, 0/0).
                     * The origin must sit on a FILLED ring. */
                    && oc0 >= 512u && oc1 >= 512u
                    && s_probe_snap_body_nz >= 1000u
                    && s_probe_ring_direct_nz >= 64u
                    /* dp140 TRUTH #13: fidx had NO quiescence check. ik1
                     * exported fidx=0 while the live witness read 21 — the
                     * oracle seeded the wrong block phase and every
                     * computed boundary diverged. */
                    && csd_probe_snap_word(1u) == ups_fidx) { ok = 1u; break; }
                csd_probe_snap_calls += 1000u;
            } while (++tries < 8u);
            s_snap_groups += 1u;
            /* dp145 REVERT: hold the freeze through verification AND the
             * accept bookkeeping below. s_probe_feedoff (and the stamps)
             * must name the row count applied at snapshot time; releasing
             * early let a resumed feeder increment s_feed_abs BEFORE the
             * accept read, counting a row into feedoff that the window
             * copy never saw. The freeze is released once, below. */
            /* dp128 REVERT: post-freeze verification. A pass torn by an
             * interrupt loses stores after the interrupt point (ia2: R and
             * cursors landed, window/ring lost, while the in-situ body
             * witness still read full from pass N-1). What the freeze left
             * on the bus is the only truth worth accepting: re-read the
             * ring section from the capture core and require content. */
            if (ok) {
                unsigned rnz = 0u, rc = 0u;
                const volatile unsigned *b = csd_probe_snap;
                if (b)
                    for (unsigned i = 264u; i < 264u + CSD_SNAP_RINGW; i++) {
                        const unsigned v = b[i];
                        if (v) rnz++;
                        rc = (rc ^ v) * 0x01000193u;
                    }
                if (rnz < 64u) { ok = 0u; csd_probe_snap_calls += 10000u; }
                s_probe_wchk_snap = rnz;   /* witness: post-freeze ring nz */
                s_snap_crc_capture = rc;   /* dp130 */
            }
            if (ok) {
                s_snap_accepts += 1u;
                /* dp137: stamp the gate's own reads into the arena ring
                 * (proven diag-visible transport) so the host can compare
                 * them against the exported cursor words 258..263. */
                {   volatile unsigned *b = csd_probe_snap;
                    if (b) {
                        const unsigned R_ = b[0];
                        const unsigned t = 264u + CSD_SNAP_RINGW
                                         + 2u * R_ * CSD_SNAP_PER_BANK;
                        if (t + 8u <= CSD_SNAP_HDR) {
                            b[t]      = 0xC5C01337u;   /* dp139 stamp tag */
                            b[t + 1u] = rd0;  b[t + 2u] = oc0;
                            b[t + 3u] = rd1;  b[t + 4u] = oc1;
                            b[t + 5u] = s_probe_snap_body_nz;
                            b[t + 6u] = s_probe_ring_direct_nz;
                            b[t + 7u] = ups_fidx;  /* dp140: live fidx */
                        }
                    }
                }
                s_snap_claimed = 0u;
                s_probe_feedoff  = s_feed_abs;
                s_probe_prepop   = kabs;   /* dp128: kabs of the ACCEPTING
                                            * pop, not the trigger pop */
                s_probe_preblocks = s_blocks;
                s_probe_blk_n = 0u;
                s_probe_headc = 999999u;
                s_probe_qw_n = 0u;
                s_probe_pop_n = 0u;
                /* dp150: anchor ch0's qpop1 rows HERE (core A, A-local
                 * s_pops[0] read) — same reset that re-bases the pev and
                 * echo rings, which core B rows in its commit path. The
                 * accepting pair's ch0 already wrote (stale base,
                 * rejected); its ch1 hits the pop_n==0 guard: symmetric
                 * loss, rows stay pair-aligned. */
                s_qpop1_base[0] = s_pops[0];
                s_diag_pops_accept[0] = s_pops[0];
                s_diag_pops_accept[1] = s_pops[1];
                s_diag_popn_accept = s_probe_pop_n;
                s_probe_pev_tot = 0u;
                s_probe_fidx = ups_fidx;
                s_pop_clock_reset = 1u;
            }
            s_snap_freeze = 0u;              /* dp145: single release point */
            /* REVERT(dp201): close the capture-attribution window. */
            if (g_csd_probe_mask & 16u) { g_csd_p_snap += csd_ups_now() - t_s;
                                          g_csd_p_snap_n++; }
        }
    }
    /* dp151 REVERT: pair-complete release. dp148 assumed the pair commits
     * ch0-then-ch1; the qpop1 per-channel fit (jd1/jf1: ch1 exact at d=0,
     * ch0 exactly +1 at EVERY boundary, BND pop clock at the commit of the
     * PRECEDING pair) proves core B can commit a pair's ch1, the feed claim
     * then opens, and core A's ch0 of the SAME pair consumes the pushed
     * ring. The flag now marks the pair until both channels of the topmost
     * pair have committed: each core's commit path clears when core A's
     * call count (consumes + repeats) has caught up with core B's pair
     * commits (>= : core A can be one call ahead when core B commits).
     * A pending feed spins on the flag; the claim path already backs off
     * (bospins valve). */
    if ((s_pops[0] + s_repeats[0]) >= s_probe_pop_n)
        s_pop_in_commit = 0u;
    return (int)v;
}

/* ---- diagnostics (read-only views for the SCPI/MIDI surface) ----------- */

unsigned csd_ups_diag_feed_enter(void) { return s_feed_enter; }
unsigned csd_ups_diag_feed_pc(void)    { return s_pc; }
unsigned csd_ups_diag_feed_in_pc(void) { return s_in_pc; }

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
unsigned csd_probe_mis0(void)  { return s_probe_mis0; }
unsigned csd_probe_mis1(void)  { return s_probe_mis1; }
unsigned csd_probe_blkdiff(void) { return s_blocks - s_probe_preblocks; }
unsigned csd_probe_qpop1(unsigned i)
{ return (s_probe_qpop1 && i < CSD_PROBE_RING) ? s_probe_qpop1[i] : 0u; }
unsigned csd_ups_diag_block_ticks_max(void) { return s_block_ticks_max; }
/* Session 14 double-buffer witnesses (REVERT with the double-buffer). */
unsigned csd_ups_diag_push_n(void)     { return ups_st ? ups_st->pushn : 0u; }
unsigned csd_ups_diag_push_over(void)  { return ups_st ? ups_st->pushov : 0u; }
unsigned csd_ups_diag_rate_calls(void)      { return s_rate_calls; }
unsigned csd_ups_diag_last_req_hz(void)     { return s_last_req_hz; }
/* REVERT(s15-diag): reconfigure attribution witnesses. */
unsigned csd_ups_diag_rc_total(void)        { return s_rc_total; }
unsigned csd_ups_diag_rc_site(unsigned i)   { return (i < 4u) ? s_rc_site[i] : 0u; }
unsigned csd_ups_diag_rc_last(void)         { return s_rc_last; }
/* REVERT(s16-diag): indexed block-granularity ring witnesses (bit5-written).
 * 0/1 = last-4 pop-block zero histograms per channel, packed 8 bits each,
 * newest in bits 0-7, saturating at 255; 2/3 = cumulative zero pops L/R;
 * 4 = last commit base; 5/6 = occ after last commit L/R; 7 = stale-region
 * overwrite count. */
unsigned csd_ups_diag_s16(unsigned i)
{
    switch (i) {
    case 0u: case 1u: {
        unsigned c = i, p = 0u;
        for (unsigned a = 0u; a < 4u; a++) {
            unsigned v = s16_blkz[c][3u - a];
            if (v > 255u) v = 255u;
            p |= v << (a * 8u);
        }
        return p; }
    case 2u: return s16_popz[0];
    case 3u: return s16_popz[1];
    case 4u: return s16_pbase;
    case 5u: return s16_pocc[0];
    case 6u: return s16_pocc[1];
    case 7u: return s16_ovlv;
    case 8u: return s16_lgg[0];
    case 9u: return s16_lgg[1];
    case 10u: return s16_lggz[0];
    default: return s16_lggz[1];
    }
}

/* 2026-11-12 THE MIC PATH IS NOT A BIT-EXACT CAPTURE: at ratio>1 the USB
 * mic endpoint (245k slots/s) samples the pop mailbox while the engine
 * publishes at wire rate (384k/s) and ALSA plughw RESAMPLES whatever the
 * device sends: measured 1-in-3 real pairs + linear-interpolation garbage
 * (the +-32 residuals) in both echo streams. Neither pop nor feed echo
 * can certify bits through that path. These rings are the honest bench
 * source: writers are the single legit producer per stream (audio core
 * for pop commits, EQ task for feed args), reader is the SCPI thread;
 * freeze-on-full pins the FIRST events, whose absolute indices the xsim
 * oracle models exactly via BASE?. Heap-allocated at probe-enable (SCPI
 * thread only) because tile DP-relative statics hit the 32 KiB window
 * next to ups_blob/kstage. Armed-only; four stores per event.
 * REVERT: delete this block, the hooks, and the PROBE/BASE/POP?/FEED?
 * handlers. */
volatile int32_t (*s_probe_pop)[2]  = 0;
volatile int32_t (*s_probe_feed)[2] = 0;
volatile unsigned s_probe_pop_n, s_probe_feed_n;
volatile unsigned s_probe_on, s_probe_base, s_probe_popbase;
volatile unsigned s_rec_on;   /* record only AFTER the clean-start reconfigure */
volatile unsigned s_probe_reconfigs;
volatile unsigned s_probe_pre;   /* feed CALLS before the first nonzero feed */
volatile unsigned s_probe_fidx;  /* engine block index when the first feed landed */
volatile unsigned s_probe_dumpfidx; /* DIAG:UPSAMPLing:DUMP? reader (REVERT) */
volatile unsigned s_abs_feed, s_abs_calls;
volatile unsigned *s_probe_fev_t;
volatile unsigned s_probe_fev_tot;
volatile unsigned *s_probe_pev_t;
volatile unsigned s_probe_pev_tot;
volatile unsigned s_feed_abs;      /* absolute feed pairs since PROBE arm */
volatile unsigned s_probe_armfidx;
/* ---- trigger-time state snapshot (2026-11-12, decisive capture) ---------
 * Everything the engine output depends on BEFORE the first scored pop:
 * fd FIR history (ups_blob), in-flight input window (ups_in), the delivery
 * ring and cursors (ups_q/rd/occ/last), and the per-phase FIFO head.
 * Copied by the trigger; replayed by the xsim oracle verbatim, which is
 * then driven ONLY by post-trigger events. No pre-trigger history is ever
 * assumed again (ev1: a ~9-feed residual from drain audio outside the
 * PROBE-anchored feed window). REVERT with the rings. */
volatile unsigned *csd_probe_snap;
volatile unsigned csd_probe_snap_words;
/* One static arena for the snapshot: the heap allocator silently
 * returned an overlapping/failed block for the 4th+ malloc (dp79:
 * snapshot fn witnessed complete, C=101, buffer all-zero). Static
 * placement removes the allocator from the equation; REVERT. */
static unsigned *s_probe_snapbuf;   /* dp121: heap (shared-visible) */
unsigned  csd_probe_snap_calls;   /* witness: did the trigger reach us? */
/* dp111 REVERT: writer-side body witnesses — separate a bad copy
 * (reads wrong source) from a bad read (reader core sees its own
 * tile-local copy of the arena). */



void csd_probe_snapshot(void)
{
    csd_probe_snap_calls++;
    {   /* dp105: is the pop core blind to task-written state? Recompute the
         * window checksum on THIS core at snapshot time and compare with the
         * running checksum feed() maintains on the task core. */
        unsigned wc = 0u;
        for (unsigned c = 0u; c < 2u; c++)
            for (unsigned k = 0u; k < CSD_UPS_FRAME_ADVANCE; k++)
                wc = (wc ^ (unsigned)ups_in[c][k]) + 0x9E3779B9u;
        s_probe_wchk_snap = wc;
        s_probe_wchk_run = s_probe_wchk;
    }
    if (!csd_probe_snap) { csd_probe_snap_calls = 1000u + csd_probe_snap_calls;
                           return; }
    unsigned w = 0u;
    const unsigned R = ups_phases;
    s_snap_entry += 1u;                      /* dp129 */
    s_snap_progress = 1u;                    /* dp129 */
    csd_probe_snap[w++] = R;
    csd_probe_snap[w++] = ups_fidx;
    s_snap_progress = 2u;                    /* dp129 */
    /* dp122 REVERT: what does THIS core see when writing the header? */
    s_witness_stptr    = (unsigned)(uintptr_t)ups_st;
    s_witness_heapok   = ups_heap_ok;
    s_witness_fidx_cap = ups_fidx;
    s_witness_winw0    = (unsigned)ups_in[0][0];
    for (unsigned c = 0u; c < 2u; c++)
        for (unsigned i = 0u; i < CSD_UPS_FRAME_ADVANCE; i++)
            csd_probe_snap[w++] = (unsigned)ups_in[c][i];
    for (unsigned c = 0u; c < 2u; c++) {
        csd_probe_snap[w++] = ups_rd[c];
        csd_probe_snap[w++] = ups_occ[c];
        csd_probe_snap[w++] = (unsigned)ups_last[c];
    }
    /* THE DELIVERY RING ITSELF (dp91 fo1): rows 0..93 matched only
     * because both sides were silent; the first non-zero device pop came
     * from ring content the oracle never had. Pop-side output begins as
     * ring replay — its CONTENTS are the nearest-past state. */
    {   /* dp113 REVERT: read the ring the SAME way the pop path does and
         * CRC it directly. If this is nonzero while the copied body below
         * is zero, the copy loop (aliasing/optimizer) is the liar; if this
         * is zero too, this core is genuinely blind to the ring. */
        /* dp119: FLAT pointer walk — dp113-dp118 proved the single-index
         * pop read sees real words while a nested [c][i] scan on the SAME
         * core reads zeros. Discriminate visibility vs nested-index codegen
         * (DP-relative addressing) by walking one base pointer. REVERT. */
        unsigned rc = 0u, rn = 0u;
        const int32_t *flat = ups_qp[0];
        for (unsigned i = 0u; i < 2u * ups_ring_w; i++) {
            unsigned v = (unsigned)flat[i];
            if (v) rn++;
            rc = (rc ^ v) * 0x01000193u;
        }
        s_probe_ring_direct_crc = rc;
        s_probe_ring_direct_nz  = rn;
    }
    {   const int32_t *fq = ups_qp[0];   /* dp119 flat walk */
        for (unsigned i = 0u; i < 2u * ups_ring_w; i++) {
            csd_probe_snap[w++] = (unsigned)fq[i];
            if ((i & 63u) == 0u) s_snap_progress = w;   /* dp129 */
        }
        s_snap_progress = 100000u + w;                   /* ring section done */
    }
    for (unsigned p = 0u; p < R; p++)
        for (unsigned c = 0u; c < 2u; c++) {
            csd_probe_snap[w++] = ups_data[p][c].head_index;
            /* dp89 OFFS witness: the vendor fd layout lives ENTIRELY in
             * the blob (headers at [0], data from +4, prev_td @262,
             * overlap @390 -> live extent 518). Dumping only
             * MAX_DATA_WORDS(456) truncated the overlap history. */
            {   const int32_t *fb = &ups_blob[p][c][0];  /* dp119 flat */
                for (unsigned i = 0u; i < CSD_SNAP_DUMP; i++) {
                    csd_probe_snap[w++] = (unsigned)fb[i];
                    if ((i & 63u) == 0u) s_snap_progress = 200000u + w; /* dp129 */
                }
            }
            /* fd struct scalars: pointers are DERIVED (vendor init math)
             * so only the three descriptors cross the wire (dp88: the
             * seed left block_count/frame_advance at 0 — compute walked
             * an uninitialized window and xsim died at 0x28220) */
            csd_probe_snap[w++] = ups_data[p][c].td_block_length;
            csd_probe_snap[w++] = ups_data[p][c].block_count;
            csd_probe_snap[w++] = ups_data[p][c].frame_advance;
            /* dp142: pad word now carries the DEVICE data offset
             * (data_blocks[0].data - blob base). Vendor init aligns the
             * data region to 8 bytes, so the offset depends on the heap
             * base alignment: device=+8, oracle=+6. Without this the
             * host copied history at device indices while the oracle
             * MAC read at its own (+2 shifted) -> every computed block
             * diverged (in1). */
            csd_probe_snap[w++] = (unsigned)(
                ((uintptr_t)ups_data[p][c].data_blocks[0].data
                 - (uintptr_t)&ups_blob[p][c][0]) / 4u);
        }
    {   /* dp111 REVERT: CRC + nonzero count of the payload region as
         * THIS core just wrote it. If nz is large at capture but the
         * host sees zeros, the reader core is looking at another copy. */
        unsigned crc = 0u, nzn = 0u;
        for (unsigned i = 264u; i < w; i++) {
            unsigned v = csd_probe_snap[i];
            if (v) nzn++;
            crc = (crc ^ v) * 0x01000193u;
        }
        s_probe_snap_body_crc = crc;
        s_probe_snap_body_nz  = nzn;
    }
    csd_probe_snap_words = w;
    csd_probe_snap_calls += 100u;   /* reached-the-end marker */
}
volatile unsigned s_pop_clock;   /* written audio core only; read by task */
volatile unsigned s_probe_prepop;
volatile unsigned csd_probe_en_stat;   /* 0 off, 1 on, 2 OOM (REVERT) */
volatile unsigned s_probe_ev_n_tot;
char s_probe_dump[24576] __attribute__((section(".dp.bss.large")));


volatile unsigned probe_heap_total;   /* dp126 witness */
void csd_probe_enable(unsigned on)             /* also resets (pre-run) */
{
    if (on && !s_probe_pop) {
        /* dp123 REVERT: single block, hand-partitioned — the allocator's
         * overlapping 4th+ blocks (dp79/ht1) can no longer alias a live
         * ring over the snapshot arena. */
        unsigned total = 0u;
        const unsigned oq = total;  total = ups_bump(total + 2u * CSD_PROBE_RING * 2u);
        const unsigned osp = total; total = ups_bump(total + CSD_SNAP_HDR);
        const unsigned opp = total; total = ups_bump(total + CSD_PROBE_RING * 2u);
        const unsigned ofp = total; total = ups_bump(total + CSD_PROBE_FRING * 2u);
        const unsigned ofe = total; total = ups_bump(total + CSD_PROBE_EV);
        const unsigned ope = total; total = ups_bump(total + CSD_PROBE_RING);
        probe_heap_total = total;              /* dp126 witness */
        unsigned *mb = ups_alloc(total * 4u);
        if (!mb) { csd_probe_en_stat = 2u; return; }
        s_probe_qpop1_buf = (unsigned (*)[CSD_PROBE_RING])(void *)(mb + oq);
        s_probe_snapbuf = mb + osp;
        s_probe_pop = (int32_t (*)[2])(void *)(mb + opp);
        s_probe_feed = (int32_t (*)[2])(void *)(mb + ofp);
        s_probe_fev_t = mb + ofe;
        s_probe_pev_t = mb + ope;
        s_probe_qpop1 = &s_probe_qpop1_buf[1][0];
        csd_probe_snap = s_probe_snapbuf;
    }
    csd_probe_en_stat = on ? 1u : 0u;
    s_probe_on = on ? 1u : 0u;
    s_pop_clock = 0u;   /* rebases with the rings (el1: a reconfigure-
                         * anchored clock carried boot history: prepop=6368) */
    s_feed_abs = 0u;    /* absolute feed history rebases with the rings */
    csd_probe_snap_words = 0u;
    if (csd_probe_snap) csd_probe_snap[0] = 0u;  /* poison: n() infers */
    s_probe_armfidx = ups_st ? ups_fidx : 0u;  /* dp121: state may not be allocated yet */  /* feed-grid phase at arm: the oracle must
                                  * resume the SAME 128-block boundary, not
                                  * restart it (measured ev1: a constant
                                  * phase offset of ~9 feeds — 165/256
                                  * membership with correct values) */
    s_probe_pop_n = s_probe_feed_n = 0u;
    s_probe_blk_n = 0u;
    s_abs_feed = s_abs_calls = 0u;
    s_probe_base = s_probe_popbase = 0u;
    s_rec_on = 0u;
}
unsigned csd_probe_en_get(void)  { return csd_probe_en_stat; }
unsigned csd_probe_prepop(void)  { return s_probe_prepop; }
unsigned csd_probe_feedabs(void) { return s_feed_abs; }
unsigned csd_probe_armfidx_get(void) { return s_probe_armfidx; }
unsigned csd_probe_snap_word0(void)
{ const volatile unsigned *b = csd_probe_snap; return b ? b[0] : 0xffffffffu; }
unsigned csd_probe_snap_n(void)
{
    /* words is trusted only if it is sane; otherwise INFER the length
     * from the buffer: word 0 must be a legal phase count and the
     * implied total must fit the header bound (ring stores across cores
     * are demonstrably visible; this scalar alone misbehaved, dp79). */
    const volatile unsigned *b = csd_probe_snap;
    if (!b) return 0u;
    const unsigned R = b[0];
    if (R >= 1u && R <= 8u && (R & (R - 1u)) == 0u) {
        const unsigned t = 264u + CSD_SNAP_RINGW + 2u * R * CSD_SNAP_PER_BANK;
        /* dp139: 8-word accept stamp lives just past the banks (inside
         * the 8-bank reservation) so the host sees what the gate read. */
        if (t + 8u <= CSD_SNAP_HDR) return t + 8u;
    }
    return csd_probe_snap_words;
}
unsigned csd_probe_snap_calls_get(void) { return csd_probe_snap_calls; }
unsigned csd_probe_snap_dataw(void) { return CSD_SNAP_PER_BANK; }
unsigned csd_probe_snap_ringsz(void) { return CSD_SNAP_RINGW; }
unsigned csd_probe_snap_word(unsigned i)
{ const volatile unsigned *b = csd_probe_snap;
  return b ? b[i] : 0xdeadbeefu; }
unsigned csd_probe_snap_dump(unsigned off, unsigned cnt)
{
    int o = 0;
    const volatile unsigned *b = csd_probe_snap;
    unsigned nn = csd_probe_snap_n();
    if (!nn) {
        const volatile unsigned *c = csd_probe_snap;
        const unsigned R = c ? c[0] : 0u;
        if (R >= 1u && R <= 8u && (R & (R - 1u)) == 0u)
            nn = 264u + CSD_SNAP_RINGW + 2u * R * CSD_SNAP_PER_BANK;
    }
    if (!b || !nn) return 0u;
    for (unsigned i = 0u; i < cnt; i++) {
        const unsigned k = off + i;
        if (k >= nn) break;
        const int wr = snprintf(s_probe_dump + o,
                                8192u - (unsigned)o,
                                "%s%u", i ? ";" : "", b[k]);
        if (wr <= 0 || (unsigned)wr >= 8192u - (unsigned)o) break;
        o += wr;
    }
    return (unsigned)o;
}
unsigned csd_probe_base(void)    { return s_probe_base; }
unsigned csd_probe_reconf(void)  { return s_probe_reconfigs; }
unsigned csd_probe_pre(void)     { return s_probe_pre; }
unsigned csd_probe_fidx(void)    { return s_probe_fidx; }
unsigned csd_probe_popbase(void) { return s_probe_popbase; }
unsigned csd_probe_dump(unsigned off, unsigned cnt, int is_pop)
{
    volatile unsigned n;
    if (is_pop == 2) {                  /* dp95: post-commit ring words (u32) */
        int o = 0;
        if (!s_probe_qpop1) return 0u;
        n = s_probe_pop_n;
        if (n > CSD_PROBE_RING) n = CSD_PROBE_RING;
        for (unsigned i = 0u; i < cnt; i++) {
            const unsigned idx = off + i;
            if (idx >= n) break;
            const int wr = snprintf(s_probe_dump + o, sizeof s_probe_dump - (unsigned)o,
                                    "%s%u", i ? ";" : "", s_probe_qpop1[idx]);
            if (wr <= 0 || (unsigned)wr >= sizeof s_probe_dump - (unsigned)o) break;
            o += wr;
        }
        return (unsigned)o;
    }
    volatile int32_t (*ring)[2] = is_pop ? s_probe_pop : s_probe_feed;
    int o = 0;
    if (!ring) return 0u;
    if (is_pop) {
        n = s_probe_pop_n;
    } else {
        const unsigned a = s_feed_abs;
        n = (a > CSD_PROBE_FRING) ? CSD_PROBE_FRING : a;
    }
    /* Pop ring: freeze-on-full window from index 0.
     * Feed ring: absolute-indexed (n % RING) — the last CSD_PROBE_RING
     * pairs since PROBE arm, oldest first; the host maps an absolute
     * sequence n to row (n - (s_feed_abs - rows_returned)). 2026-11-12. */
    const unsigned base = is_pop
        ? ((n > CSD_PROBE_RING) ? (n - CSD_PROBE_RING) : 0u)
        : (s_feed_abs - n);   /* feed rows are ring slots base..base+n */
    for (unsigned i = 0u; i < cnt; i++) {
        const unsigned idx = off + i;                 /* row in the window  */
        if (idx >= n) break;
        const unsigned k = (base + idx)
                         % (is_pop ? CSD_PROBE_RING : CSD_PROBE_FRING);
        const int w0 = ring[k][0], w1 = ring[k][1];
        const int wr = snprintf(s_probe_dump + o, sizeof s_probe_dump - (unsigned)o,
                                "%s%d,%d", i ? ";" : "", w0, w1);
        if (wr <= 0 || (unsigned)wr >= sizeof s_probe_dump - (unsigned)o) break;
        o += wr;
    }
    return (unsigned)o;
}
const char *csd_probe_dump_buf(void) { return s_probe_dump; }
unsigned csd_probe_ev_count(void)
{
    /* Must cover every kind the dump emits: 0/1 events, 2 state words,
     * 3 qpop1, 4 ring-write stamps. */
    unsigned n = s_probe_fev_tot + s_probe_pev_tot;
    const unsigned R = csd_probe_snap_word0();
    if (R >= 1u && R <= 8u && (R & (R - 1u)) == 0u) {
        unsigned sn = 264u + CSD_SNAP_RINGW + 2u * R * CSD_SNAP_PER_BANK
                    + 8u;                  /* dp140: accept stamp tail */
        if (sn > CSD_SNAP_HDR) sn = CSD_SNAP_HDR;
        n += sn;
    }
    n += 2u * s_probe_pop_n;
    n += 64u;
    return n;
}
unsigned csd_probe_ev_dump(unsigned off, unsigned cnt)
{
    /* Feed-producer events first (kind 0), then pop-producer (kind 1);
     * the host merges the two single-writer streams by timestamp. */
    int o = 0;
    for (unsigned i = 0u; i < cnt; i++) {
        const unsigned k = off + i;
        if (k >= CSD_PROBE_EV || k >= s_probe_fev_tot) break;
        const unsigned x = s_probe_fev_t[k];
        const int wr = snprintf(s_probe_dump + o, 24576u - (unsigned)o,
                                "%s0,%u,%u", i ? ";" : "",
                                k, x);
        if (wr <= 0 || (unsigned)wr >= 24576u - (unsigned)o) break;
        o += wr;
    }
    for (unsigned i = 0u; i < cnt; i++) {
        const unsigned k = off + i;
        if (k >= CSD_PROBE_RING || k >= s_probe_pev_tot) break;
        const int wr = snprintf(s_probe_dump + o, 24576u - (unsigned)o,
                                "%s1,%u,%u", o ? ";" : "", k,
                                s_probe_pev_t[k]);
        if (wr <= 0 || (unsigned)wr >= 24576u - (unsigned)o) break;
        o += wr;
    }
    /* STATE words ride the proven EV channel as (2, index, value):
     * the dedicated STATE? reader misbehaved (dp79-dp85 saga). */
    {
        const unsigned R = csd_probe_snap_word0();   /* proven reader only */
        if (R >= 1u && R <= 8u && (R & (R - 1u)) == 0u) {
            unsigned sn = 264u + CSD_SNAP_RINGW + 2u * R * CSD_SNAP_PER_BANK
                        + 8u;              /* dp140: accept stamp tail */
            if (sn > CSD_SNAP_HDR) sn = CSD_SNAP_HDR;   /* dp87 fj1: an
                 * unclamped estimate read past the static arena and the
                 * oracle replayed 0xdeadbeef payloads into fd blocks */
            if (s_snap_crc_diag == 0u && sn > 264u) {
                /* dp130: latch the reader-core's view of the ring section */
                unsigned rc = 0u;
                for (unsigned i = 264u; i < 264u + CSD_SNAP_RINGW; i++)
                    rc = (rc ^ csd_probe_snap_word(i)) * 0x01000193u;
                s_snap_crc_diag = rc ? rc : 1u;
            }
            for (unsigned i = 0u; i < cnt; i++) {
                const unsigned k = off + i;
                if (k >= sn) break;
                const int wr = snprintf(s_probe_dump + o,
                                        24576u - (unsigned)o,
                                        "%s2,%u,%u", o ? ";" : "", k,
                                        csd_probe_snap_word(k));
                if (wr <= 0 || (unsigned)wr >= 24576u - (unsigned)o) break;
                o += wr;
            }
        }
    }
    /* dp96: qpop1 rides the same channel as (3, index, value) — the
     * dedicated QPOP? reader misbehaved exactly like STATE? did. */
    for (unsigned i = 0u; i < cnt; i++) {
        const unsigned k = off + i;
        if (k >= CSD_PROBE_RING || k >= s_probe_pop_n) break;
        const int wr = snprintf(s_probe_dump + o, 24576u - (unsigned)o,
                                "%s3,%u,%u", o ? ";" : "", k,
                                s_probe_qpop1_buf[0][k]);
        if (wr <= 0 || (unsigned)wr >= 24576u - (unsigned)o) break;
        o += wr;
    }
    for (unsigned i = 0u; i < cnt; i++) {
        const unsigned k = off + i;
        if (k >= CSD_PROBE_RING || k >= s_probe_pop_n) break;
        const int wr = snprintf(s_probe_dump + o, 24576u - (unsigned)o,
                                "%s5,%u,%u", o ? ";" : "", k,
                                s_probe_qpop1_buf[1][k]);
        if (wr <= 0 || (unsigned)wr >= 24576u - (unsigned)o) break;
        o += wr;
    }
    /* dp100: (4, n, popclock) — when each of the first 64 ring writes of a
     * scored refill happened. Tells host which writer filled pre-boundary
     * rows. */
    for (unsigned i = 0u; i < cnt; i++) {
        const unsigned k = off + i;
        if (k >= 64u) break;
        const int wr = snprintf(s_probe_dump + o, 24576u - (unsigned)o,
                                "%s4,%u,%u", o ? ";" : "", k,
                                s_probe_qwpop[k]);
        if (wr <= 0 || (unsigned)wr >= 24576u - (unsigned)o) break;
        o += wr;
    }
    return (unsigned)o;
}
