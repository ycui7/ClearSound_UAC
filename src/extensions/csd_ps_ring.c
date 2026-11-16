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

#define CSD_PS_RING 8u                    /* power of two                  */
#define CSD_PS_LEAD (CSD_PS_RING - 2u)    /* bounded producer lead         */

/* ring words: producer writes; consumer reads after the seq cursor moves. */
static int      s_w0[CSD_PS_RING];
static int      s_w1[CSD_PS_RING];
/* Cursors are NON-static: the inline-asm ldw references them by symbol
 * (dp-relative), which needs file-global linkage — same idiom as the
 * g_digData reads in xua_audiohub. Opacity to the parallel-usage checker
 * comes from living in a C module, not from storage class. */
unsigned g_csd_ps_seq      = 0u;   /* pairs published (producer writes)    */
unsigned g_csd_ps_consumed = 0u;   /* pairs claimed   (consumer writes)    */
static unsigned s_produced_local = 0u; /* producer-private copy            */
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
extern int      csd_ups_pop_active(unsigned ch, int w); /* REVERT(dp198) */
extern unsigned csd_ups_active(void);

static inline unsigned ps_ldw_seq(void)
{ unsigned v; asm volatile("ldw %0, dp[g_csd_ps_seq]" : "=r"(v)); return v; }
static inline unsigned ps_ldw_consumed(void)
{ unsigned v; asm volatile("ldw %0, dp[g_csd_ps_consumed]" : "=r"(v)); return v; }

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
unsigned g_csd_ub_seq = 0;
int      g_csd_ub_pair[2] = {0, 0};
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
void csd_ub_consume_if_new(void)
{
    unsigned sq;
    asm volatile("ldw %0, dp[g_csd_ub_seq]" : "=r"(sq));
    if ((sq & 1u) != 0u || sq == s_ub_last) { return; }
    unsigned l0, l1, chk;
    asm volatile("ldw %0, dp[g_csd_ub_pair]"   : "=r"(l0));
    asm volatile("ldw %0, dp[g_csd_ub_pair+4]" : "=r"(l1));
    asm volatile("ldw %0, dp[g_csd_ub_seq]"    : "=r"(chk));
    if (chk != sq) { return; }             /* wire re-opened: retry next */
    s_ub_last = sq;
    unsigned pair[2] = { l0, l1 };
    unsigned t_u = (g_csd_probe_mask & 16u) ? csd_ups_now() : 0u;  /* REVERT(dp201) */
    UserBufferManagement(pair, s_ub_mic);
    if (g_csd_probe_mask & 16u) { g_csd_p_ubm += csd_ups_now() - t_u;  /* REVERT(dp201) */
                                  g_csd_p_ubmn++; }
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
    unsigned t_p = (s_pass_pm & 16u) ? csd_ups_now() : 0u;   /* REVERT(dp201) */
    unsigned w0 = (unsigned)csd_ups_pop_active(0u, 0);   /* REVERT(dp198) */
    unsigned w1 = (unsigned)csd_ups_pop_active(1u, 0);
    if (s_pass_pm & 16u) { g_csd_p_pop2 += csd_ups_now() - t_p;  /* REVERT(dp201) */
                                  g_csd_p_passn++; }
    /* REVERT(dp207): publish without the two cross-tile LOADS the
     * `seq += 1` RMWs implied. WHY: armed 192k needs one producer pass per
     * wire frame (384,000/s = 260 t at 100 MHz) and, once dp206 made the
     * occupancy exact, the ring no longer hid a slow pass: measured passes
     * 379,776/s (263 t) with UBM on every second pass -> 1.1% chronic
     * shortfall, ps_fresh 343.8k vs wire_n 383.9k (10% honest holds).
     * The producer is the ONLY writer of the mailbox sequence, so it can
     * carry the counter in a register and issue two plain STORES: the
     * odd/even window the reader validates is preserved exactly. Saves the
     * two shared-memory reads (~15-20 t each) off the zero-margin pass. */
    s_pub_seq += 1u;
    g_csd_ps_mbox_seq = s_pub_seq;            /* odd = window open  */
    g_csd_ps_mbox_w0 = (int)w0;
    g_csd_ps_mbox_w1 = (int)w1;
    s_pub_seq += 1u;
    g_csd_ps_mbox_seq = s_pub_seq;            /* even = pair complete */
    s_produced_local++;
    g_csd_ps_seq = s_produced_local;          /* pass rate (PACE?) */
    if ((s_act_n++ & 15u) == 0u) { g_csd_ps_active = csd_ups_active(); }
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
unsigned g_csd_ps_active  = 0;
static unsigned s_claim_n = 0;         /* REVERT(dp187): active refresh div */

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
            s_latch0 = s_w0[i];
            g_csd_ps_latch1 = s_w1[i];
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
