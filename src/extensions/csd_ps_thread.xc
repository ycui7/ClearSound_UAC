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


/* csd_ps_ring.c */
void     csd_ps_produce_pair(void);
void     csd_ub_consume_if_new(void);  /* REVERT(dp200-wire-diet) */
unsigned csd_ps_fresh_get(void);    /* REVERT(dp196): wire fresh clock */
unsigned csd_ps_wire_get(void);     /* REVERT(dp198): wire frame beat */
extern unsigned g_csd_p_err, g_csd_p_dwl, g_csd_p_ptkxi; /* REVERT(dp198) */
extern unsigned g_csd_p_tdl, g_csd_p_pndl;               /* REVERT(dp198) */
/* csd_ups_block.c */
unsigned csd_ups_armed(void);
unsigned csd_ups_diag_out_hz(void);
/* REVERT(dp185): segment witnesses (defined in xua_audiohub.xc TU; producer
 * fills [6]=whole pass, [7]=produce_pair when probe mask bit4 set). */
extern unsigned g_csd_probe_mask;
extern unsigned g_csd_seg[8];

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
    /* REVERT(dp196): rate-adaptive stride state (16.16 ticks + carry). */
    unsigned ptkx = 260u << 16, raccx = 0u, n256 = 0u, fresh_snap = 0u;
    /* REVERT(dp198): window anchors for the wait-effectiveness witnesses. */
    unsigned t_anchor = 0u, pn_anchor = 0u;
    while (1)
    {
        g_csd_psw_alive++;
        if (!csd_ups_armed())
        {
            unsigned now; pt :> now;
            pt when timerafter(now + 100000u) :> void;   /* 1 ms idle */
            pf      = 0u;   /* dp175: force re-latch next arm */
            continue;
        }
        /* REVERT(dp188): PROBE mask bit6 (DEBUG:CSD:PROBE 64) parks the
         * producer at runtime — attribution probe for the dp187 wall: the
         * wire loop sits at a reproducible 326.0 t/frame (306.75k/s,
         * +-85/s across masks AND builds) while SEG prices floor+claims
         * at ~72 t over the 261 t machine base. Park producer -> claim
         * starves (honest repeat) -> if the wire rate jumps, the producer
         * core/thread contends with the wire loop; if not, the wall is
         * inside the wire thread. */
        if (g_csd_probe_mask & 64u)
        {
            unsigned now; pt :> now;
            pt when timerafter(now + 100000u) :> void;
            continue;
        }
        /* REVERT(dp184): per-pass witness increments (psw_armed/lead/tmr/
         * prod) and the cadence stores (p_pnext/p_now/ptk/slip) pruned —
         * this thread shares the tile's issue bandwidth with the wire
         * loop; every op here competes with the pop it exists to move off
         * the critical path. alive+latched still prove liveness. */
        /* REVERT(dp175): the dp161 'latch once per arm' froze ptk at
         * whatever out_hz read during the FIRST window (measured p_ptk =
         * 1,198,935 = the 48k drain rate) -> producer free-ran at machine
         * max with RELAX firing 642k/s (p_force). Track the rate every
         * pass; re-anchor the schedule on any change. */
        {
            /* REVERT(dp198): rate track hoisted to /16 (~12 t/pass off the
             * core; the cross-TU out_hz call ran EVERY pass since dp175).
             * Rate changes ride stream transitions; 16 passes ~= 50 us
             * adoption is 4 orders under any stream-start transient. */
            static unsigned nrate = 16u;
            if (++nrate >= 16u)
            { nrate = 0u;
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
                /* REVERT(dp196): seed the rate-adaptive stride and rebase
                 * the fresh window (see the pass below). */
                ptkx  = (unsigned)(((unsigned long long)65536ull *
                                    XS1_TIMER_HZ) / pf);
                raccx = 0u; n256 = 0u; fresh_snap = csd_ps_fresh_get();
                { unsigned nwl; pt :> nwl;
                  t_anchor = nwl; pn_anchor = pnext; }   /* REVERT(dp198) */
            }
            }
        }
        /* REVERT(dp189): lead-wait + RELAX machinery removed with the
         * claim path — the mailbox is depth-1, last-pair-wins, so there
         * is no ring to protect from an over-running producer (see
         * csd_ps_ring.c produce_pair). The pass is now: wait on the wire
         * deadline, produce, publish. g_csd_ps_consumed/lead witnesses
         * freeze at their boot values (the claim path is the only reader
         * of consumed and it is off the hot path). */
        /* REVERT(dp185): PURE deadline accumulator (was dp178
         * wait-then-anchor — same defect as the wire floor: anchoring to
         * the observed wake time adds wake-latency+work to every period,
         * so an early producer self-paced at machine rate instead of the
         * wire stride). A loop that finishes before the next deadline is
         * paced exactly by '+= ptk'; the lead-yield loop above already
         * bounds the late/lead-full case. slip/pnow witnesses and the
         * per-pass timer read pruned with it. */
        unsigned seg6a; if (g_csd_probe_mask & 16u) pt :> seg6a;
        pt when timerafter(pnext) :> void;
        /* REVERT(dp196): pnext accumulates the ADAPTIVE stride (was the
         * fixed ptk/prm/racc grid — dp195 grid_probe: the fixed grid
         * free-ran at nominal while the wire consumed slower, and the
         * ring paid the difference in repeats). prm/racc kept defined
         * for the dp174 witnesses only. */
        pnext += (ptkx >> 16);
        raccx += (ptkx & 0xFFFFu);
        if (raccx >= 65536u) { pnext++; raccx -= 65536u; }
        /* REVERT(dp196): RATE-ADAPTIVE stride. The free grid (dp161,
         * measured dp195: pops+reps == 384,037/s against a port-paced wire
         * at 375-380k) over-drains the ring by the grid-vs-wire delta, and
         * every excess pop is a hold-last repeat on the wire. Cross-core
         * SEQ cursors failed three builds (.63/.64/.65 — see the wire-side
         * comment in xua_audiohub.xc). What survives is a counter: the wire
         * stws g_csd_ps_fresh once per frame whose delivered words MOVED
         * (single writer, no torn state). The producer closes the loop at
         * 256-pass windows: 256 passes must buy 256 fresh accepts; deficit
         * slows, surplus accelerates, bang-bang on a 16.16 fixed-point
         * stride (1/65536-tick authority — integer ticks at 260 t are
         * 0.38%, 20x coarser than the 0.02% residual being removed).
         * Equilibrium: passes == fresh accepts == wire frames == fill rate
         * => ring drain == fill EXACTLY on average; the producer leads by
         * ~one pair so the wire never starves. DC-content publishes
         * (word-unchanged) are not accepts: the producer then idles at the
         * clamp and the wire honest-repeats — correct for DC.
         * One timer wait per pass preserved (dp161 wall rule). */
        {   if (++n256 >= 256u)
            {   n256 = 0u;
                /* REVERT(dp198): beat target = WIRE FRAMES, not fresh
                 * accepts. The dp196 fresh-semantics target is
                 * unsatisfiable by construction: a fresh accept consumes a
                 * publish, publishes == passes, so fresh <= passes and
                 * err = 256-delta >= 0 forever -- the controller could
                 * only ever slow down and sat pinned at the 290 t upper
                 * clamp (measured armed 192k leg: passes 467 k/s free-run,
                 * ps_fresh 0, producer 205 t/pass over-draining the
                 * upsampler queue into 48-frame zero bursts every ~3 ms).
                 * Wire frames are the true anchor: the L-site accepts one
                 * pair per I2S frame regardless of movement, so the fixed
                 * point passes == wire-frames == fill rate holds in both
                 * directions. K = 1/16 tick per frame of error: the
                 * limit-cycle width around the exact seed stride
                 * (100e6/out_hz, already 260.416 t) stays under 0.1 t. */
                unsigned w = csd_ps_wire_get();
                long delta = (long)(w - fresh_snap);   /* now a wire beat */
                fresh_snap = w;
                /* REVERT(dp198): wait-effectiveness witnesses — timer time
                 * vs schedule time across one 256-pass window. If
                 * tdl >> pndl*... the waits are sleeping (stride-paced);
                 * if tdl == passes*work (pndl tracks tdl exactly with
                 * stride pinned low), every wake is late (free-run).
                 * Cost: two timer reads per 256 passes, zero on the pass. */
                { unsigned nwd; pt :> nwd;
                  g_csd_p_tdl = nwd - t_anchor; t_anchor = nwd;
                  g_csd_p_pndl = pnext - pn_anchor; pn_anchor = pnext; }
                long err = delta - 256l;               /* wire ahead -> speed up */
                if (err >  64l) err =  64l;
                if (err <  -64l) err = -64l;
                long v = (long)ptkx - ((err * 65536l) >> 4l);
                /* REVERT(dp201): lower clamp 240 -> 200. Measured armed
                 * 192k leg (flashed 1d9688b2): wire accepts at port pace
                 * 384,313/s while passes saturate at 362,149/s with the
                 * stride pinned at 240 (p_err +16 forever): the pass's own
                 * work (~241 t: pop pair 136 t + UBM 53 t on 53 % of
                 * passes + overhead) plus wake latency makes the effective
                 * cycle ~276 t REGARDLESS of the stride floor, so the old
                 * clamp gave the controller no authority to lead the wire.
                 * The producer then under-fills the mailbox ~6-15 % of
                 * frames (fresh/s 325k vs wire_n 384k): those reads are
                 * the honest 1-frame holds that cap the mic FFT harmonics
                 * at ~-31 dB. 200 t still bounds over-drain authority below
                 * the fill rate once the loop closes (surplus decelerates
                 * on the same gain; dp196 pathology needs passes>FILL,
                 * which the beat servo now removes from both sides). */
                if (v < (long)(200l << 16)) v = (long)(200l << 16);
                if (v > (long)(290l << 16)) v = (long)(290l << 16);
                ptkx = (unsigned)v;
                g_csd_p_err   = (unsigned)err;
                g_csd_p_dwl   = (unsigned)delta;
                g_csd_p_ptkxi = ptkx >> 16;            /* REVERT(dp198) */
            }
        }
        unsigned seg7a; if (g_csd_probe_mask & 16u) pt :> seg7a;
        /* REVERT(dp200-wire-diet): ORDER IS LOAD-BEARING -- pop FIRST,
         * then the deferred UBM feed. UBM feeds the eq in-ring (tile[1]
         * eq task consumes, publishes to the resampler queue that
         * produce_pair drains). Feed-before-pop deadlocks the thread with
         * the queue full: the feed's backpressure blocks the only pop
         * that could relieve it (measured: producer pinned 1.4k pass/s,
         * eq_overrun +1/pass, eq task frozen, mailbox stuck hold-last).
         * Pop-first drains the out end every pass before the feed can
         * block it. */
        csd_ps_produce_pair();
        csd_ub_consume_if_new();
        if (g_csd_probe_mask & 16u)
        { unsigned seg7b; pt :> seg7b;
          g_csd_seg[7] += seg7b - seg7a; g_csd_seg[6] += seg7b - seg6a; }
    }
}
