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
void     csd_ps_rearm_reset(void);  /* REVERT(dp209): per-arm cursor reset */
int      csd_ub_consume_if_new(void);   /* REVERT(dp210): 1 = consumed */  /* REVERT(dp200-wire-diet) */
unsigned csd_ps_fresh_get(void);    /* REVERT(dp196): wire fresh clock */
unsigned csd_ps_wire_get(void);     /* REVERT(dp198): wire frame beat */
extern unsigned g_csd_p_err, g_csd_p_dwl, g_csd_p_ptkxi; /* REVERT(dp198) */
extern unsigned g_csd_p_tdl, g_csd_p_pndl;               /* REVERT(dp198) */
/* csd_ups_block.c */
unsigned csd_ups_armed(void);
unsigned csd_ups_diag_out_hz(void);
unsigned csd_ps_lead(void);   /* REVERT(dp210): burst gate (csd_ps_ring.c) */
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

/* dp223 2026-11-23 (owner: 'our code stacked on our own threads; never
 * burden the vendor timed path'): the producer's REAL home is a tile[1]
 * top-level par thread (user_main.h CSD_PS_TILE1_ENTRY) where the slot,
 * the claim queue and the engine ring are all LOCAL data space — the
 * tile[0] copy saw ubmn=129 (dead) vs the wire-claim's 158k. The vendor
 * main.xc dp161 branch still calls csd_ps_thread() on tile[0]; that is
 * this no-op shell, so exactly ONE producer runs and NO vendor line moves.
 * REVERT: rename back and re-populate the tile[0] branch. */
extern unsigned g_csd_psw_pass, g_csd_psw_post, g_csd_psw_seed, g_csd_psw_pre;
extern unsigned g_csd_pt_drain, g_csd_pt_prod;   /* dp224c (csd_ps_ring.c) */
int csd_ps_lead_c(void);   /* dp224d (csd_ps_ring.c) */
void csd_ps_pass(void);
void csd_ps_note_clock(unsigned tv);   /* dp225f (csd_ps_ring.c) */      /* dp225b (csd_ps_ring.c): whole pass, one call */
void csd_ups_pop_pair(unsigned *unsafe w0, unsigned *unsafe w1);  /* csd_ups_block.c (C ptr: *unsafe to match) */
void csd_ps_active_refresh(void);  /* dp225b (csd_ps_ring.c) */
extern unsigned csd_ups_get_arm_snap(unsigned idx);
extern void csd_ups_force_reconfig(void);   /* dp223d (csd_ps_ring.c) */
void csd_ps_thread(void)
{
    /* dp225o: producer body moved to csd_ps_thread_t1 (tile[1]). This
     * tile[0] task is PARKED (main.xc spawns it unconditionally). Sleep
     * on a timer — never busy-spin (it would steal the XUD core). */
    timer pk_tmr; unsigned pkv = 0u;
    while (1)
    {   select { case pk_tmr when timerafter((pkv + 100000000u) & ~1u) :> void: break; }
        pk_tmr :> pkv;
    }
}

void csd_ps_thread_t1(chanend cePop, chanend ceUbR)
{
    /* dp225o 2026-11-24: producer MOVED BACK to its own tile[1] core.
     * Fresh evidence after the publish ring (dp225m) and ring widen
     * (dp225n): host delivers EXACTLY 192k frames/s (filled delta =
     * 458,752 B/s, refusals 0), publish ring feeds the engine the full
     * 192k/s, engine mints 2x = 384k/s is REQUIRED, but consumed froze
     * at wakes x lead (246 pass/s x 1022 = 251.7k/s). The pass rate is
     * par-scheduling-starved on tile[0] (XUD + decouple saturate that
     * core: 32k packets/s + 193.5k token exchanges/s). dp209's rejection
     * of tile[1] predates both fixes and measured the POP cost, not the
     * wake rate — the wake starvation is the binding constraint now.
     * tile[1] hosts wire on one core; this task gets the other. All our
     * DS (claim ring, engine, publish-ring reader cursor) is tile[1]
     * local. The pass sleeps timerafter(now+100) so a core-sharing
     * schedule stays cooperative. Channels unused (legacy plumbing). */
    /* channels intentionally unused */
    unsigned arm_seen = 0u, arm_stable = 0u;
    csd_ups_force_reconfig();
    unsigned act_div = 0u;
    timer pt_yield;
    unsigned ptv = 0u;
    while (1)
    {
        pt_yield :> ptv;          /* dp225p: spin, no sleep (own core) */
        csd_ps_note_clock(ptv);   /* dp225f: feed-rate estimator (C side) */
        g_csd_psw_alive++;   /* dp225b: liveness heartbeat */
        unsigned ar = csd_ups_armed();
        if (ar != arm_seen)
        {   arm_seen = ar; arm_stable = 0u; }
        else if (ar != 0u && arm_stable < 8u)
        {   if (++arm_stable == 8u) { csd_ups_force_reconfig(); } }

        if ((act_div++ & 7u) == 0u) { csd_ps_active_refresh(); }

        csd_ps_pass();

        g_csd_psw_pass++;
    }
}


/* dp225 CHANNEL DATA-PLANE (owner policy: our code on our threads; vendor
 * timed loop gains ONLY select handlers). Root cause of the r2 ~125k/s
 * ceiling was NOT the exchange: the wire core and this producer both touch
 * the shared-memory queue/engine cursors every iteration, and cross-tile
 * SDRAM accesses (hundreds of cycles each, arbitrated against the owning
 * tile) put ~8 us on BOTH a wire iteration and a producer pass (pt phases:
 * 448 t of actual work inside a 7.6 us pass). Channels are the vendor's own
 * hot-path idiom: hardware mailbox, zero DS contention.
 *
 * g_csd_ch_pop : producer -> wire, one word per outuint (L then R).
 * g_csd_ch_ubl/r : wire publish -> producer, L and R on SEPARATE chanends
 *   so the wire tries both in ONE select with default (never blocks). */
extern void csd_pw_note_host_pair(int l, int r);  /* csd_dsp.xc: stream-alive witness (FUNCTIONAL) */
extern unsigned g_csd_psw_alive;

extern unsigned g_csd_ps_consumed;                /* csd_ps_ring.c: FD? feed witness (producer writes) */
extern void csd_ups_feed(int l, int r);           /* csd_ups_block.c */
unsigned csd_ups_active(void);

