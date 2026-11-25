/*
 * csd_ps_ring.c — dp161 2026-11-13: async wire-word producer ring (data half).
 *
 * WHY (measured, dp160): the armed audiohub loop is compute-bound ABOVE the
 * wire-timer floor (437 ticks/iteration vs a 260-tick floor at 384 kHz), so
 * the floor never binds and LRCLK/ADC/mic-shipping self-pace at 0.61-0.80x
 * wire while USB consumes at the host rate -> ~39% zero-filled mic stream,
 * time-warped sample instants, split tone + 32k/Nyquist images. The last
 * ~66 ticks of the deficit are the two csd_ups_slot() pop calls, dominated
 * by the UNCONDITIONAL pop-path diagnostics inside csd_ups_block.c
 * (off-limits). Attribution proof: short-circuiting the two pop calls
 * (CSD_PROBE_NOSLOT) locks the loop at 384,131 it/s — floor binding.
 * dp161 moves the pops to a producer thread; the wire loop only claims
 * already-produced pairs (a handful of loads per frame).
 *
 * SPLIT (dp161 lesson #2): the XC parallel-usage checker resolves cross-unit
 * calls and polices the globals their bodies touch, so the whole producer/
 * consumer pair cannot live in .xc. C function bodies are OPAQUE to it —
 * the mechanism csd_eq_ring.c and the csd_diag handoffs already rely on.
 * All data lives and is touched HERE (C); the cadence owner is
 * csd_ps_thread.xc, which touches no globals itself — it just waits on its
 * timer and calls csd_ps_produce_pair(). One core, cooperative par: SPSC,
 * single writer per cursor, ring depth 8, bounded lead 6 — a published slot
 * is never recycled under the reader. Cursors are read through asm ldw so
 * neither thread's optimiser hoists them across its waits.
 *
 * REVERT(dp161 2026-11-13): delete this file + csd_ps_thread.xc, the
 * externs + csd_ps_thread() par branch + two csd_ps_claim call sites in
 * xua_audiohub.xc; restore direct csd_ups_slot calls under ratio>1.
 */
#include <xs1.h>

#ifndef XS1_TIMER_HZ
#define XS1_TIMER_HZ 100000000u           /* 10 ns XS1 timer ticks        */
#endif

#define CSD_PS_RING 1024u                  /* dp225n 2026-11-24: 128 -> 1024.
                                           * Supply math (measured, feed ring
                                           * dp225m in place): producer wakes
                                           * only ~1966/s (par scheduling —
                                           * the wire task barely blocks) so
                                           * supply = wakes x lead. Lead 126
                                           * capped supply at 249.7k pair/s =
                                           * consumed EXACTLY (wire demands
                                           * 384k). Ring 1024 + lead 1022
                                           * gives 1966x1022 = 2.0M/s of
                                           * headroom; latency cost 2.6 ms,
                                           * irrelevant for the FD chain.
                                           * power of two. Original note:
                                           * producer pays ~100 t of wake
                                           * latency per timer wait; at the
                                           * r2 pass rate (one pass per wire
                                           * frame) that puts the shared
                                           * core ~32 % over budget and the
                                           * truth pass (PROBE 48, true
                                           * window) measured the result:
                                           * passes 291,270/s vs 383,986
                                           * wire claims = 95,541/s honest
                                           * holds (24.9 % duplicate
                                           * frames) = the 122.88 Hz comb.
                                           * The queue ring lets ONE wake
                                           * top up a burst of pairs and
                                           * then sleep LEAD/2 strides,
                                           * amortising the wake x30 (12.8k
                                           * wakes/s instead of 384k).
                                           * power of two                */
#define CSD_PS_LEAD (CSD_PS_RING - 2u)    /* bounded producer lead         */

/* ring words: producer writes; consumer reads after the seq cursor moves.
 * REVERT(dp209): de-stated for the inline wire-side claim (dp189's mailbox
 * experiment is over: last-wins has NO repeat counter and its phase beat
 * against the producer publish grid is the periodic duplicate-frame source
 * = the 122.88 Hz r2 comb. Queue semantics with a consumed cursor make the
 * duplicate visible (g_csd_ps_starve) and rate-free. Both threads are
 * tile[0] (main.xc dp161 branch + audiohub) -> local DS access either way.
 * REVERT: re-static when the mailbox is restored. */
int      g_csd_ps_w0[CSD_PS_RING];
int      g_csd_ps_w1[CSD_PS_RING];
/* Cursors are NON-static: the inline-asm ldw references them by symbol
 * (dp-relative), which needs file-global linkage — same idiom as the
 * g_digData reads in xua_audiohub. Opacity to the parallel-usage checker
 * comes from living in a C module, not from storage class. */
unsigned g_csd_wire_target_hz = 0u;  /* dp225f: producer-estimated 2x host feed rate (wire floor pacing) */
unsigned g_csd_ps_seq = 0u;   /* pairs published (producer writes)    */
unsigned g_csd_ps_consumed = 0u;   /* pairs claimed   (consumer writes)    */
unsigned s_produced_local = 0u; /* producer-private copy            */
/* REVERT(dp207): producer-private publish sequence (see csd_ps_produce_pair).
 * Single writer = the producer; the two stores below keep the odd/even
 * window the reader validates, without paying the shared-memory LOADs the
 * `g_csd_ps_mbox_seq += 1` RMWs implied on the zero-margin pass. */ 
static unsigned s_pub_seq = 0u;
extern unsigned g_csd_ps_active;   /* REVERT(dp189): defined below; the
                                    * producer refreshes it in produce_pair */
unsigned g_csd_ps_produced  = 0;   /* witnesses (monotone, SCPI-visible)   */
unsigned g_csd_ps_repeat    = 0;   /* consumer saw lead > ring (anomaly)   */
unsigned g_csd_ps_starve    = 0;   /* consumer found nothing new           */
unsigned g_csd_ps_overwrite = 0;   /* producer exceeded lead, published    */

/* application side (csd_ups_block.c) */
extern int      csd_ups_slot(unsigned ch, int w);
extern int      csd_ups_pop_active(unsigned ch, int w);
void csd_ups_pop_pair(unsigned *w0, unsigned *w1);
unsigned csd_ups_out_empty(void);   /* dp225f (csd_ups_block.c): 1 = no fresh out word */   /* REVERT(dp210) */ /* REVERT(dp198) */
extern unsigned csd_ups_active(void);

static inline unsigned ps_ldw_seq(void)
{ return g_csd_ps_seq; }   /* dp223c: C canonical */
static inline unsigned ps_ldw_consumed(void)
{ return g_csd_ps_consumed; }   /* dp223c */

/* Cadence-owner helpers (called from csd_ps_thread.xc) */
unsigned csd_ps_lead(void)          { return s_produced_local - ps_ldw_consumed(); }
void     csd_ps_note_overwrite(void){ g_csd_ps_overwrite++; }

/* One wire pair: pop L then R, publish after the words. Called once per
 * wire frame by the cadence owner. */
/* REVERT(dp189): mailbox publish. The wire loop reads g_csd_ps_mbox_*
 * inline (seqlock; single writer = this thread, single reader = the
 * audiohub wire thread). WHY: the claim path — even the dp187 trimmed
 * form — kept a wall at exactly 326.0 t/frame (dp187/dp188 rate_probe:
 * 306.75k/s +-85/s across claim-skip, witness, SEG and producer-park
 * masks; only bit1 floor-skip moved it, to the 383.6k port-paced rate).
 * Pop cadence (this thread's wire-stride timer) and pop ORDER (L then R)
 * are unchanged from the oracle model; the wire side just reads the
 * latest published pair instead of calling into this module. seq is even
 * when a pair is complete (words are written before the seq store; xcore
 * stores are atomic, so the reader never sees a half pair as complete:
 * a seq move between the reader's two seq reads = torn read -> retry).
 * g_csd_ps_active is refreshed here (every 16th pass) so the wire sites
 * read a global instead of calling csd_ups_active() (dp187 hoist moved
 * further off the wire thread; bounded 16-frame enable transient, same
 * rule dp185 already accepted). */
/* REVERT(dp196): stores via asm stw. The only READERS of these variables
 * are hand-asm dp[sym] ldw's in xua_audiohub.xc — invisible to the
 * optimiser's use graph — and the plain C stores were being silently
 * dropped (witness .70: the pop path delivered a tone into the mic mirror,
 * produce_pair's own w0 witness read 0, the wire fresh clock was frozen,
 * ECHO-2 real wire audio was garbage). Publishing through asm stw makes
 * the stores unremovable and pairs with the wire's asm ldw. (XC forbids
 * volatile scalars; the heap migration of dp121 stays the fallback if
 * per-core views are proven next.) */
unsigned g_csd_ps_mbox_seq = 0u;   /* even = pair complete */
int      g_csd_ps_mbox_w0  = 0;
int      g_csd_ps_mbox_w1  = 0;
/* REVERT(dp196): RATE-LOCK clocks. g_csd_ps_fresh is incremented by the
 * WIRE thread (asm in xua_audiohub.xc L-site) once per frame whose
 * delivered pair actually MOVED; the producer paces its stride so 256
 * passes buy 256 fresh accepts (csd_ps_thread.xc). Grid proof chain:
 * dp195 grid_probe (free grid): pops 383.5k/s vs fill 379.1k/s; the three
 * cross-core seq-cursor forms (.63/.64/.65) all mis-counted consumption
 * through seqlock races — a plain counter has no such states. */
unsigned g_csd_ps_fresh = 0u;
/* dp216 witnesses — DEFINED HERE (this TU's tile[1] copy is the one the
 * diag task reads: g_csd_wire_n/g_csd_ps_fresh proven live). audiohub
 * (xua_audiohub.xc) increments via externs. A definition in the .xc file
 * lands in the tile[0] DS copy and reads 0 — re-proven 2026-11-23. */
unsigned g_csd_wl_pass = 0;    /* wire-claim guard iterations (L loop) */
unsigned g_csd_psw_pre = 0;    /* dp223e producer pre-wait */
unsigned g_csd_psw_post = 0;   /* dp223d producer post-wait */
unsigned g_csd_psw_seed = 0;   /* dp223d producer re-seeds */
unsigned g_csd_pt_drain = 0, g_csd_pt_prod = 0, g_csd_pt_rest = 0;  /* dp224c pass-phase cycles */
int csd_ps_lead_c(void)   /* dp224d: lead via .c (XC par-checker ignores C bodies) */
{
    return (int)(s_produced_local - g_csd_ps_consumed);
}

unsigned g_csd_hb = 0;       /* dp224: host-frame flag (set by vendor c_aud complete) */
unsigned g_csd_ub_race = 0;   /* dp224: seqlock re-reads */
unsigned g_csd_psw_pass = 0;   /* dp223d producer pass counter */
unsigned g_csd_wl_claim = 0;   /* g_csd_ps_active claim entries */
unsigned g_csd_fb_freeze = 0;  /* dp225h: read by ep_buffer.xc (feedback freeze while armed) */
unsigned g_csd_wl_emit = 0;    /* DAC emit reached */
unsigned g_csd_wl_drain = 0;   /* inline drain: seq seen != cursor */
unsigned g_csd_wl_acc = 0;     /* inline drain: pairs accepted+fed */
unsigned g_csd_wl_pass2 = 0;   /* dp216b: R (post-exchange) claim guard entries */
unsigned g_csd_wl_r2 = 0;      /* dp216b: g_csd_usb_ratio via asm dp-relative */
unsigned g_csd_wl_claim2 = 0;  /* dp216b: R-site ps_active claim entries */
/* REVERT(dp196): wire-side attribution witnesses (written in
 * xua_audiohub.xc L-site accept path; read via PACE?). */
unsigned g_csd_wire_l0, g_csd_wire_seen, g_csd_wire_s1, g_csd_wire_s2,
         g_csd_wire_n;
/* REVERT(dp196): producer-side view witnesses + core identities. */
unsigned g_csd_prod_w0, g_csd_prod_n, g_csd_prod_tile, g_csd_wire_tile;
static unsigned s_act_n = 0;

unsigned csd_ps_fresh_get(void)
{ unsigned v; asm volatile("ldw %0, dp[g_csd_wire_n]" : "=r"(v)); return v; }

/* REVERT(dp198): controller beat source. Same counter, plain C read -- the
 * asm ldw dp[] form above was the dp196 controller's blind spot: with the
 * producer free-running (every wake late, no effective wait) the windowed
 * delta still drove the OLD fresh-semantics target, which is unsatisfiable
 * (fresh <= publishes == passes) and ratcheted the stride to the 290 t
 * clamp. dp198 paces passes == wire frames on this beat instead. */
unsigned csd_ps_wire_get(void) { return g_csd_wire_n; }
/* REVERT(dp198): controller attribution witnesses (csd_ps_thread.xc;
 * printed by DIAG:UPSAMPLing:PACE? tail). */
unsigned g_csd_p_err = 0, g_csd_p_dwl = 0, g_csd_p_ptkxi = 0;
unsigned g_csd_p_tdl = 0, g_csd_p_pndl = 0;   /* REVERT(dp198): wait-effect */

/* REVERT(dp200-wire-diet): deferred UBM hand-off (armed legs, ECHO 2).
 * WHY: the SEG/xt witnesses price UserBufferManagement at ~115 t per
 * exchange (xt_ubm, armed 192k leg) inside the wire thread's gate-true
 * frame -- with the LRCLK port saturated at 64 BCLK/frame, wire work
 * above the 260.4 t stride keeps the loop one half-word late no matter
 * where inside the iteration it sits (pop-skip freed 18 t: rate pinned).
 * UBM is the DSP feed (host pair -> eq ring -> upsampler queue) plus the
 * idle-power stream witness and preset/gen adoption; at ECHO 2 it writes
 * NO mic slots (the port decimator publishes the ADC words directly), so
 * it runs on the producer core, once per exchange, consume-once via this
 * seqlock (odd = wire mid-hand-off; cross-thread reads/writes all
 * ldw/stw per the dp199 no-hoist rule). The host pair is a COPY: the
 * wire re-uses samplesOut for the mailbox pop. ECHO 0/1 and ratio-1 keep
 * the wire-side UBM call (stock path, passT leg untouched). */
unsigned g_csd_ub_seq = 0;   /* dp223c: plain (XC linker bans cross-TU volatile mismatch); wire loop has blocking port ops = reload barrier */
int g_csd_ub_pair[2] = {0, 0};
/* dp225m 2026-11-24: PUBLISH RING (128 pairs). Measured with the burst fix
 * (dp225l): XUD commits EXACTLY 458,752 B/s = 192k frames/s (filled delta,
 * refusals 0) yet the drain consumed only 140.2k/s. Mechanism: last-wins
 * single slot + drain wakes at the pass cadence (4387/s = 228 us); ~44
 * publishes elapse between passes and the slot keeps only the LAST —
 * ~28% of host frames were overwritten unread. The dp210 note below
 * ("the fix is a cadence match, not a new queue") assumed a feeder at
 * 192k/s that never survived (feeder thread retired dp225 era). With the
 * drain pinned to the pass grid by cooperative par, a ring is REQUIRED.
 * Wire-side cost: 5 plain stores per exchange (193.5k/s), same sanctioned
 * handoff region. Wrap-safe: writer never lapses the reader (lead gate on
 * the drain side bounds at RING). REVERT: delete ring, restore slot reads. */
#define CSD_UB_RING 128u
int g_csd_ub_ring[CSD_UB_RING * 2u];
unsigned g_csd_ub_rseq = 0;   /* even counter; pair index = (rseq>>1)&127 */
unsigned g_csd_ub_lapdrop = 0; /* REVERT(dp225m): ring-lap drops (should stay 0) */
/* dp210 2026-11-23: NO host-pair queue here. The single-slot seqlock is
 * lib_xua's own handoff (xua_audiohub_st.h csd_ub_defer_handoff) and the
 * lib_xua buffers (decouple outAudioBuff / the eq in-ring) are the FIFOs.
 * A last-wins slot drops frames ONLY when the consumer wakes slower than
 * the publisher (the batch producer at 12.8k wakes/s against a 192k/s
 * publish — p_ubmn 20k/s truth). The fix is a cadence match, not a new
 * queue: csd_ub_feeder_thread() (csd_ps_thread.xc) consumes on the host
 * grid (one publish, one consume, ~192k/s) and nothing is lost. If a
 * future change ever makes the feeder slower than the publish rate it
 * MUST consume lib_xua's own buffers, never a hand-rolled ring. */
void UserBufferManagement(unsigned sampsFromUsbToAudio[],
                          unsigned sampsFromAudioToUsb[]);
static unsigned s_ub_last = 0u;
static unsigned s_ub_mic[2] = {0u, 0u};   /* ECHO 2 never writes it */
/* REVERT(dp201 2026-11-13): producer-pass attribution, PROBE bit4 gated.
 * pop2/passn: both pops from produce_pair (excludes the pop-internal
 * regions timed separately in csd_ups_block.c — together they bracket the
 * pop cost). ubm/ubmn: the UserBufferManagement call actually made
 * (consume-once: ubmn counts real calls, so ubm/ubmn = t/call). PACE?
 * fields 31-42. */
unsigned g_csd_p_pop2 = 0, g_csd_p_passn = 0;
unsigned g_csd_p_ubm = 0, g_csd_p_ubmn = 0;
extern unsigned csd_ups_now(void);
extern unsigned g_csd_probe_mask;
extern volatile unsigned s_pass_pm;   /* csd_ups_block.c: per-pass mask
                                       * snapshot (s16 close-out lean-gate) */
extern void csd_ups_feed(int l, int r);
extern void csd_pw_note_host_pair(int l, int r);
extern unsigned csd_ub_feed_owner;   /* csd_ups_block.c (dp215: def moved to tile[1] DS) */
int csd_ub_consume_if_new(void)   /* REVERT(dp210): 1 = consumed one pair this
                                     * call — batch producer drains to 0 */
{
    /* dp223c: PLAIN C on volatile canonical objects — no dp[] asm anywhere.
     * dp is a THREAD register: dp-relative asm inside a function reached
     * from another tile's task resolves against the CALLER task's data
     * segment, so publisher and consumer each hit their own tile's copy
     * (measured: ub_seq 3.38M in one copy, drain frozen at boot prefill
     * in the other). C references take the linked canonical address;
     * cross-tile traffic is remote memory, which xcommon handles. */
    unsigned sq = g_csd_ub_rseq;
    if (sq == s_ub_last) { return 0; }
    /* dp225m: ring read (see the ring decl). Never lapped in practice
     * (pass gap ~44 pairs << 128); the clamp keeps a wedge from a long
     * stall honest (drop old frames, keep the stream live). */
    unsigned r = s_ub_last;
    if ((sq - r) > (CSD_UB_RING * 2u - 2u)) { r = (sq - (CSD_UB_RING * 2u - 2u)) & ~1u; g_csd_ub_lapdrop++; }
    unsigned ri = ((r >> 1) & (CSD_UB_RING - 1u)) * 2u;
    unsigned l0 = (unsigned)g_csd_ub_ring[ri];
    unsigned l1 = (unsigned)g_csd_ub_ring[ri + 1u];
    s_ub_last = r + 2u;
    unsigned pair[2] = { l0, l1 };
    unsigned t_u = (g_csd_probe_mask & 16u) ? csd_ups_now() : 0u;  /* REVERT(dp201) */
    /* dp223: call the FUNCTIONAL core directly (stream witness + FIR feed),
     * not the full UserBufferManagement body — that body's armed fast path
     * now republishes the slot (wire-side publisher role), which from here
     * would self-spin. Mic-mirror / preset-adopt belong to the pop-side
     * publish (csd_dsp.xc armed block) and the native path. */
    csd_pw_note_host_pair((int)pair[0], (int)pair[1]);
    csd_ups_feed((int)pair[0], (int)pair[1]);
    (void)s_ub_mic;
    if (g_csd_probe_mask & 16u) { g_csd_p_ubm += csd_ups_now() - t_u;  /* REVERT(dp201) */
                                  g_csd_p_ubmn++; }
    return 1;
}

void csd_ps_produce_pair(void)
{
    /* REVERT(dp179): producer was parked for the direct-pop experiment;
     * dp183 attribution (DEBUG:CSD:PROBE mask) proved the in-loop pop
     * costs 78 t/frame against a 260 t wire budget — direct pop can never
     * wire-lock. Re-engaged by dp184: the wire loop claims pairs via
     * csd_ps_claim(), pop ORDER (L then R) and cadence (wire-stride timer
     * in csd_ps_thread.xc) are unchanged from the direct-pop oracle model.
     * REVERT(dp189): claim replaced by the mailbox above. */
    /* REVERT(dp198): hot-path cost cut — the dp196 witness stores
     * (prod_w0/prod_n/prod_tile via get_local_tile_id every pass, ~15 t),
     * the dead claim ring (s_w0/s_w1: csd_ps_claim has had no caller since
     * dp189) and the redundant ps_produced counter are off the pass.
     * ps_seq still counts passes (== old prod_n). PACE? reports the
     * witnesses at their last-written values. */
    /* Session 16 close-out: ONE mask read per pass (dp204 lean-gate rule)
     * published for the pop path's gated sites (s_pass_pm). */
    s_pass_pm = g_csd_probe_mask;
    /* REVERT(dp209): DEMAND-DRIVEN PRODUCTION (queue semantics restored).
     * The pass is now: [lead < LEAD_MAX] ? pop+publish : skip. The lead is
     * measured against the WIRE's consumed cursor, so the producer can
     * never over-drain ups_q (the free-running publish of dp189-dp208 let
     * the demand chain pop faster than the wire consumed: surplus pairs
     * were silently overwritten in the last-wins mailbox and the FD feed
     * chain re-consumed non-advanced host input). The publish cursor
     * g_csd_ps_seq is the claim source compared by the wire (exact store,
     * NOT /16 — the /16 witness diet stays only for ps_seq diagnostics).
     * The mbox_* publish is deleted: the wire no longer reads it; PACE
     * reports the last frozen values (ps_fresh now counts CLAIMS). */
    {   unsigned lead = s_produced_local - ps_ldw_consumed();
        if (lead >= CSD_PS_LEAD)
        {   /* Ring full: the wire is the pacer. No pop, no publish —
             * ups_q waits with us. Witness keeps the pass ladder alive. */
            if ((s_act_n++ & 15u) == 0u) { g_csd_ps_active = csd_ups_active(); }
            return;
        }
    }
    /* dp225f: DON'T PUBLISH REPEATS. When the engine's out-queue is empty
     * pop_core returns the held word — publishing it spends a ring slot, a
     * wire claim, and the pair's SDRAM traffic to deliver a sample the wire
     * would honestly repeat anyway. ~27% of pass throughput was spent
     * minting repeats (measured: 181k pairs/s of which ~55k repeats). */
    if (csd_ups_out_empty() != 0u)
    {   if ((s_act_n++ & 15u) == 0u) { g_csd_ps_active = csd_ups_active(); }
        return;
    }
    unsigned t_p = (s_pass_pm & 16u) ? csd_ups_now() : 0u;   /* REVERT(dp201) */
    unsigned w0, w1;
    csd_ups_pop_pair(&w0, &w1);   /* REVERT(dp210): was two csd_ups_pop_active */
    if (s_pass_pm & 16u) { g_csd_p_pop2 += csd_ups_now() - t_p;  /* REVERT(dp201) */
                                  g_csd_p_passn++; }
    {   unsigned i = s_produced_local & (CSD_PS_RING - 1u);
        g_csd_ps_w0[i] = (int)w0;
        g_csd_ps_w1[i] = (int)w1;
    }
    s_produced_local++;
    g_csd_ps_seq = s_produced_local;   /* claim cursor (wire compares this) */
    if ((s_produced_local & 15u) == 0u) { g_csd_ps_produced = s_produced_local; }
    if ((s_act_n++ & 15u) == 0u) { g_csd_ps_active = csd_ups_active(); }
    /* dp210 2026-11-23: consume-once FOLDED INTO THE PASS (gold semantics:
     * the deferred-UBM slot was drained once per pass). A burst wake
     * interleaves its consumes across ~20-40 us while the wire publishes
     * LIVE publishes; publishes that landed during the sleep window were
     * already collapsed by last-wins before the wake (the measured
     * p_ubmn 20k/s deficit) — the burst period was halved (30 -> 15
     * strides) so half of every window is covered. NEVER a hand-rolled
     * queue: cadence and the lib_xua buffers own this. */
    csd_ub_consume_if_new();
}

/* REVERT(dp209): re-arm cursor reset. Called by csd_ps_thread's unarmed idle
 * branch (the wire is pass-through while unarmed — g_csd_ps_active 0 — so
 * single-threaded here). Without this, a re-arm inherits stale cursors and
 * the first frames claim yesterday's words. */
void csd_ps_rearm_reset(void)
{
    s_produced_local = 0u;
    g_csd_ps_seq     = 0u;
    g_csd_ps_consumed = 0u;
    s_ub_last        = 0u;   /* stale host pair must not re-feed */
}

/* Consumer — audiohub wire loop, ratio>1 only. ch 0 = L half-frame claims
 * the next pair (non-blocking; on underflow REPEAT the latched pair and
 * count the starve); ch 1 = R half-frame returns the latch's second word.
 * Not engaged (transient before the blob engages): stock pass-through via
 * csd_ups_slot, which returns w untouched. */
static int s_latch0 = 0;               /* consumer-private */
/* REVERT(dp187): active flag and R word exported for the R site — the
 * audiohub R claim was a cross-TU call whose body was one register return
 * (dp186 attribution: the pair of claim calls cost ~30 t/frame). Same
 * thread, same iteration: the L claim below writes both before the R site
 * reads them. */
int      g_csd_ps_latch1  = 0;
unsigned g_csd_ps_active = 0;
static unsigned s_claim_n = 0;         /* REVERT(dp187): active refresh div */

int csd_ps_claim_r(void)
{
    /* dp225f: R half-frame view. The L claim this iteration latched the
     * pair; R returns the second word WITHOUT touching the cursor. Inactive
     * = stock pass-through handled at the site by csd_ups_slot. */
    if (!g_csd_ps_active) { return 0; }
    return g_csd_ps_latch1;
}

int csd_ps_claim(unsigned ch, int w)
{
    if (ch == 0u)
    {
        /* REVERT(dp187): csd_ups_active() only flips on cold-path client
         * enable/disable; refresh every 16th claim (≈42 us at 384 kHz) —
         * a mid-flip costs at most a 16-frame word, same bounded-transient
         * rule the dp185 hoist already accepted. */
        if ((s_claim_n++ & 15u) == 0u) { g_csd_ps_active = csd_ups_active(); }
        if (!g_csd_ps_active) { return csd_ups_slot(0u, w); }
        unsigned want = g_csd_ps_consumed;
        unsigned seq = ps_ldw_seq();
        if (seq != want)
        {
            unsigned i = want & (CSD_PS_RING - 1u);
            s_latch0 = g_csd_ps_w0[i];
            g_csd_ps_latch1 = g_csd_ps_w1[i];
            g_csd_ps_consumed = want + 1u;
            if (seq - want > CSD_PS_RING) { g_csd_ps_repeat++; }
        }
        else
        {
            g_csd_ps_starve++;   /* repeat: wire outran the producer */
        }
        return s_latch0;
    }
    /* REVERT(dp187): ch==1 path unused since the R site reads
     * g_csd_ps_latch1 directly; kept so a revert is one line at the site. */
    if (!g_csd_ps_active) { return csd_ups_slot(1u, w); }
    return g_csd_ps_latch1;
}

/* dp225b 2026-11-24: whole producer pass folded into ONE call. Measured
 * psw_pass free-run ~117k/s: each pass paid a dozen cross-TU/C calls plus
 * SDRAM-arbitrated accesses against the wire's core traffic; the feed then
 * capped below the 192k host rate (engine under-fed -> honest repeats ->
 * the comb). Inside THIS TU the cursors are local objects: drain bursts
 * and produce bursts cost one call per pass, not per pair. */
void csd_ps_pass(void)
{
    unsigned k;
    for (k = 0u; k < 512u; k++)
    {   int c = csd_ub_consume_if_new();
        if (c == 0) { break; }
        if (c < 0)  { (void)csd_ub_consume_if_new(); }   /* one race retry */
    }
    /* dp225l: burst/lead 63 -> 127. Measured pass ceiling 4387/s (par
     * scheduling): 63 x 4387 = 276k pair/s = the observed consumed rate
     * EXACTLY — the producer was burst-capped, not supply-capped. The wire
     * at true 384k it/s claims 384k/s; 127 x 4387 = 557k covers it with
     * ring-128 wrap-safety intact (lead never reaches 128). */
    for (k = 0u; k < CSD_PS_LEAD; k++)
    {   if ((s_produced_local - ps_ldw_consumed()) >= CSD_PS_LEAD) { break; }
        csd_ps_produce_pair();
    }
}

/* dp225b: .c accessor for the active refresh (XC parallel checker ignores C
 * bodies; direct write from the producer .xc trips the checker). */
/* csd_ups_active declared at file top */
void csd_ps_active_refresh(void)
{   g_csd_ps_active = csd_ups_active();
    /* dp225h: freeze the USB feedback at its nominal stream-start value
     * while armed. MEASURED: host delivers 176413 frames/s = 192000 x
     * 44100/48000 (5 digits) -> the SOF-measured fb behaves 44.1/48
     * family-misclassified; supply is 8.1% short of nominal, feeding the
     * starve-repeat comb. Freeze -> host keeps nominal 192k while armed;
     * releases on disarm so native legs keep true async tracking. */
    g_csd_fb_freeze = g_csd_ps_active ? 1u : 0u;
}

/* dp225f: producer-clock feed-rate estimator. Called from the producer's
 * .xc loop with its timer read; all shared reads/writes live here (plain
 * C) so the XC parallel checker never sees cross-task globals. */
void csd_ps_note_clock(unsigned tv)
{
    extern unsigned g_csd_ft1;         /* csd_ups_block.c: feed-past-gate */
    static unsigned est_pass, est_t0, est_f0;
    if (est_pass == 0u) { est_t0 = tv; est_f0 = g_csd_ft1; }
    if (++est_pass >= 2048u)
    {   unsigned dt = tv - est_t0;
        unsigned df = g_csd_ft1 - est_f0;
        if (dt > 0u && df > 0u)
        {   /* dp225f: EXACTLY 2 x feed-pair rate (r2: 2 wire frames per
             * host pair). The mic FFT runs on the HOST clock, so the wire
             * must run at 2 x host-pair-rate for the upsampled tone to land
             * at f0; any other stride pitch-shifts the DAC output. */
            unsigned long long hz = 2ull * ((unsigned long long)df * 100000000ull) / dt;
            /* dp225f: MAX-HOLD with 1%/window decay. A plain tracking
             * estimator self-collapses: the floor slows the wire, the
             * exchange (and therefore the feed) slows with it, the
             * estimator sees a lower feed rate and lowers the floor
             * further — measured death spiral to 28k feed / silence.
             * The TRUE host rate is the MAX attainable feed; decay lets
             * it follow slow host drift. */
            if (hz < 96000u) { hz = 0u; }
            if (hz > 400000u) { hz = 400000u; }
            {   /* dp225g: estimator is OBSERVER ONLY — its tracking
                 * spiralled (wire slows -> feed slows -> floor lowers).
                 * The floor is set deterministically in the vendor loop. */
                g_csd_wl_pass2 = (unsigned)hz;   /* FD witness: measured 2x feed Hz */
            }
        }
        est_pass = 0u;
    }
}
