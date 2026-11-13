/*
 * csd_ps_thread.xc — dp161 2026-11-13: cadence owner for the wire-word
 * producer (see csd_ps_ring.c for WHY). Owns the timer and NOTHING ELSE:
 * no globals are touched in this file, so the XC parallel-usage checker
 * sees a thread that touches no shared objects; all data lives behind C
 * functions (opaque to the checker) in csd_ps_ring.c. Runs in audiohub's
 * internal par, same core, cooperative: every wait is a timer.
 *
 * Cadence: identical stride + remainder-carry to the consumer's wire floor,
 * so the pop event sequence matches the bit-perfect oracle model. Unarmed /
 * ratio<=1: sleeps 1 ms per pass; the two consume sites in xua_audiohub.xc
 * are ratio>1 gated, so the stock path stays byte-identical.
 *
 * REVERT(dp161 2026-11-13): delete this file (and csd_ps_ring.c) plus the
 * par branch + externs + two csd_ps_claim call sites in xua_audiohub.xc.
 */
#include <xs1.h>

#define CSD_PS_LEAD 6u            /* must equal CSD_PS_RING-2 in the .c    */
#define CSD_PS_RELAX 400u         /* timer yields before publishing anyway */

/* csd_ps_ring.c */
void     csd_ps_produce_pair(void);
unsigned csd_ps_lead(void);
void     csd_ps_note_overwrite(void);
/* csd_ups_block.c */
unsigned csd_ups_armed(void);
unsigned csd_ups_diag_out_hz(void);

void csd_ps_thread(void)
{
    timer pt;
    unsigned pnext = 0u, ptk = 0u, prm = 0u, racc = 0u, pf = 384000u;
    unsigned relax = 0u, latched = 0u;
    while (1)
    {
        if (!csd_ups_armed())
        {
            unsigned now; pt :> now;
            pt when timerafter(now + 100000u) :> void;   /* 1 ms idle */
            latched = 0u;
            relax   = 0u;
            continue;
        }
        if (!latched)
        {
            pf = csd_ups_diag_out_hz();
            if (pf == 0u) { pf = 384000u; }
            ptk = XS1_TIMER_HZ / pf;
            prm = XS1_TIMER_HZ % pf;
            pt :> pnext;
            latched = 1u;
        }
        while (csd_ps_lead() >= CSD_PS_LEAD)
        {
            /* Consumer behind: yield with a timer, never spin the core.
             * Bounded: after CSD_PS_RELAX yields publish anyway — lead 6
             * vs ring 8 makes this a true anomaly witness, and holding the
             * cadence matters more than dropping a pair. */
            unsigned now; pt :> now;
            pt when timerafter(now + 20u) :> void;
            if (++relax > CSD_PS_RELAX) { csd_ps_note_overwrite(); break; }
        }
        pt when timerafter(pnext) :> void;
        pnext += ptk;
        racc  += prm;
        if (racc >= pf) { pnext++; racc -= pf; }
        csd_ps_produce_pair();
    }
}
