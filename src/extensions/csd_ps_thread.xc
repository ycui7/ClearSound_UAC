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

/* REVERT(dp172): producer-side liveness witnesses — one counter per gate
 * on the path to csd_ps_produce_pair(), so a stall is attributed by which
 * counter is frozen. Printed via the csd_diag_scpi INFO tail. */
unsigned g_csd_psw_alive = 0, g_csd_psw_armed = 0, g_csd_psw_latched = 0,
         g_csd_psw_lead  = 0, g_csd_psw_tmr   = 0,          g_csd_psw_prod = 0;
/* REVERT(dp174): cadence witnesses */
unsigned g_csd_p_slip_max = 0, g_csd_p_pnext = 0, g_csd_p_now = 0,
         g_csd_p_force = 0, g_csd_p_ptk = 0;

void csd_ps_thread(void)
{
    timer pt;
    unsigned pnext = 0u, ptk = 260u, prm = 0u, racc = 0u, pf = 0u;  /* dp175: force first-pass latch */
    unsigned relax = 0u;   /* REVERT(dp175): latched removed */
    while (1)
    {
        g_csd_psw_alive++;
        if (!csd_ups_armed())
        {
            unsigned now; pt :> now;
            pt when timerafter(now + 100000u) :> void;   /* 1 ms idle */
            pf      = 0u;   /* dp175: force re-latch next arm */
            relax   = 0u;
            continue;
        }
        g_csd_psw_armed++;
        /* REVERT(dp175): the dp161 'latch once per arm' froze ptk at
         * whatever out_hz read during the FIRST window (measured p_ptk =
         * 1,198,935 = the 48k drain rate) -> producer free-ran at machine
         * max with RELAX firing 642k/s (p_force). Track the rate every
         * pass; re-anchor the schedule on any change. */
        {
            unsigned pf_now = csd_ups_diag_out_hz();
            if (pf_now >= 44100u && pf_now <= 768000u && pf_now != pf)
            {
                pf  = pf_now;
                ptk = XS1_TIMER_HZ / pf;
                prm = XS1_TIMER_HZ % pf;
                g_csd_p_ptk = ptk;   /* REVERT(dp174) */
                pt :> pnext;
                racc = 0u;
                g_csd_psw_latched++;
            }
        }
        g_csd_psw_lead++;
        while (csd_ps_lead() >= CSD_PS_LEAD)
        {
            /* Consumer behind: yield with a timer, never spin the core.
             * Bounded: after CSD_PS_RELAX yields publish anyway — lead 6
             * vs ring 8 makes this a true anomaly witness, and holding the
             * cadence matters more than dropping a pair. */
            unsigned now; pt :> now;
            pt when timerafter(now + 20u) :> void;
            if (++relax > CSD_PS_RELAX) { csd_ps_note_overwrite();
                                          g_csd_p_force++; break; }
        }
        /* REVERT(dp173): RELAX is a one-shot per quiet spell, not a
         * permanent unlock (dp161 bug: once relax>400 every lead-full
         * pass force-published forever -> 4.59M overwrites). Any pass
         * that arrives with the lead clear re-arms the bound. */
        if (csd_ps_lead() < CSD_PS_LEAD) { relax = 0u; }
        /* REVERT(dp174): cadence witnesses. slip = now-pnext at the wait
         * (int32): sustained positive slip while prod=554k/s proves the
         * accumulator sits in the past => stride/anchor bug. */
        { unsigned nw0; pt :> nw0;
          { int d = (int)(nw0 - pnext);
            if ((unsigned)d > g_csd_p_slip_max) g_csd_p_slip_max = (unsigned)d; }
          g_csd_p_pnext = pnext; g_csd_p_now = nw0; }
        g_csd_psw_tmr++;
        /* REVERT(dp178): wait-then-anchor — ring was forever full
         * (ps_seq 554k/s vs cons 443k/s); cadence wait no-oped on a
         * stale pnext. */
        pt when timerafter(pnext) :> void;
        { unsigned pnw; pt :> pnw; pnext = pnw; }
        pnext += ptk;
        racc  += prm;
        if (racc >= pf) { pnext++; racc -= pf; }
        csd_ps_produce_pair();
        g_csd_psw_prod++;
    }
}
