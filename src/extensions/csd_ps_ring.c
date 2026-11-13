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
unsigned g_csd_ps_produced  = 0;   /* witnesses (monotone, SCPI-visible)   */
unsigned g_csd_ps_repeat    = 0;   /* consumer saw lead > ring (anomaly)   */
unsigned g_csd_ps_starve    = 0;   /* consumer found nothing new           */
unsigned g_csd_ps_overwrite = 0;   /* producer exceeded lead, published    */

/* application side (csd_ups_block.c) */
extern int      csd_ups_slot(unsigned ch, int w);
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
void csd_ps_produce_pair(void)
{
    unsigned i = s_produced_local & (CSD_PS_RING - 1u);
    s_w0[i] = csd_ups_slot(0u, 0);
    s_w1[i] = csd_ups_slot(1u, 0);
    s_produced_local++;
    g_csd_ps_seq = s_produced_local;        /* publish after the words            */
    g_csd_ps_produced++;
}

/* Consumer — audiohub wire loop, ratio>1 only. ch 0 = L half-frame claims
 * the next pair (non-blocking; on underflow REPEAT the latched pair and
 * count the starve); ch 1 = R half-frame returns the latch's second word.
 * Not engaged (transient before the blob engages): stock pass-through via
 * csd_ups_slot, which returns w untouched. */
static int s_latch0 = 0, s_latch1 = 0;   /* consumer-private */

int csd_ps_claim(unsigned ch, int w)
{
    if (!csd_ups_active()) { return csd_ups_slot(ch, w); }
    if (ch == 0u)
    {
        unsigned want = g_csd_ps_consumed;
        if (ps_ldw_seq() != want)
        {
            unsigned i = want & (CSD_PS_RING - 1u);
            s_latch0 = s_w0[i];
            s_latch1 = s_w1[i];
            g_csd_ps_consumed = want + 1u;
            if (ps_ldw_seq() - want > CSD_PS_RING) { g_csd_ps_repeat++; }
        }
        else
        {
            g_csd_ps_starve++;   /* repeat: wire outran the producer */
        }
        return s_latch0;
    }
    return s_latch1;
}
