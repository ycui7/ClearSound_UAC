/*
 * btn_deglitch_sim.xc -- XSIM simulation of the PRODUCTION button FSM
 * wrapper (the select loop inside AudioHwRemoteTile0, clearsounddac_board.xc)
 * on REAL XS1 timers, with pad-edge events delivered by a scheduled
 * injection case instead of pin transitions.
 *
 * Why injection: this toolchain cannot drive xsim input ports from outside
 * (xrun here is xTAG-only; xsim has no --io portset), and xmap forbids
 * declaring one port number as both out and in. REAL in this sim: timer
 * hardware (three timers, unsigned timerafter arithmetic), the select
 * statement with guard-armed cases in production case-order, task
 * scheduling, and the shared decision functions -- this includes the
 * PRODUCTION header button_pair_fsm.h, not a copy. MODELLED: the pad
 * (shadow bits) and the edge source (the inject case runs the same
 * statements the pinsneq case runs; the pinsneq cases themselves are
 * compiled for shape parity and stay cold).
 *
 * MIRROR DISCIPLINE: the edge/window/hold case-bodies mirror the production
 * cases verbatim (clearsounddac_board.xc, the block commented "Button
 * volume"). Production change to those bodies must be reflected here; the
 * pure-FSM harness btn_pair_fsm_test.xc covers the decision table, this
 * file covers timing/selection against real timers.
 *
 * Scenarios (times relative to each scenario start, ms @ 100 MHz):
 *  D1 bounce press:      1 -> 0 @+5 -> 1 @+10, release @+120   => STEP_UP
 *  D2 sub-window spike:  high 5..25                              => (nothing)
 *     (the OLD production code emitted a phantom step here: it acted on the
 *      edge-latched value; the 2026-11-16 re-sample fix must emit nothing)
 *  D3 clean press:       high 0..60                              => STEP_UP
 *  D4 combo w/ bounce:   up @0, dn @200, up bounces 400..415,
 *                        release both @2000                      => STEP_UP, TOGGLE
 *  D5 early release:     up @0, dn @150, release both @950       => STEP_UP only
 *     (combo entry from the FIRST press stepping is the approved plan (b))
 *
 * Output: "SIM <EVENT> t=<ticks>" per action, ASSERT lines, then
 * "BTNDEGL-DONE rc=<n>".
 *
 *  COPYRIGHT (C) SPECIALTYCIRCUITS LLC 2026
 */
#include <platform.h>
#include <xs1.h>
#include <print.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include "button_pair_fsm.h"

#ifdef BTN_SIM_FAST
/* xsim runs ~200k ticks/s wall; shrink the tick scale 100x. Semantics are
 * in RATIOS (window 50 ms-equivalent, hold 1 s-equivalent, bounce gaps),
 * all preserved. Production builds never define BTN_SIM_FAST. */
#define BTN_DEBOUNCE_TICKS  50000u      /* 50 ms-equivalent */
#define MS(n)               ((n) * 1000u)
#else
#define BTN_DEBOUNCE_TICKS  5000000u    /* 50 ms @ 100 MHz -- production value */
#define MS(n)               ((n) * 100000u)
#endif
/* mirror of the production definition (clearsounddac_board.xc): 1 s */
#define BTN_HOLD_TICKS      (20u * BTN_DEBOUNCE_TICKS)

#define BTN_PORT_UP   XS1_PORT_1M
#define BTN_PORT_DOWN XS1_PORT_1N
on tile[0]: in buffered port:1 p_butt_up   = BTN_PORT_UP;
on tile[0]: in buffered port:1 p_butt_down = BTN_PORT_DOWN;

/* ---- event log ---------------------------------------------------------- */
#define MAX_EVENTS 32
static unsigned ev_kind[MAX_EVENTS];
static unsigned ev_n = 0;

static void log_ev(unsigned kind)
{
    if (ev_n < MAX_EVENTS) ev_kind[ev_n] = kind;
    ev_n++;
    printf("SIM ");
    printf(kind == BTN_PAIR_EV_STEP_UP    ? "STEP_UP"   :
             kind == BTN_PAIR_EV_STEP_DOWN  ? "STEP_DOWN" : "TOGGLE");
    printf("" "\n");
}

/* ---- stimulus schedule (strictly increasing absolute ticks) ------------- */
#define MAX_INJ 24
static unsigned inj_t[MAX_INJ], inj_mask[MAX_INJ];
static unsigned inj_n = 0;

static void sched(unsigned t, unsigned mask)
{
    if (inj_n < MAX_INJ) { inj_t[inj_n] = t; inj_mask[inj_n] = mask; }
    inj_n++;
}

static unsigned pad_up = 0, pad_dn = 0;   /* the modelled pad */

/* ---- assertions ---------------------------------------------------------- */
static int rc = 0;
static unsigned expect_idx = 0;

static void expect_seq(const char *id, const unsigned *kinds, unsigned n)
{
    unsigned ok = (ev_n >= expect_idx + n);
    for (unsigned i = 0; ok && i < n; i++)
        if (ev_kind[expect_idx + i] != kinds[i]) ok = 0;
    printf("ASSERT "); printf(id);
    printf("  want[");
    for (unsigned i = 0; i < n; i++) { printf("%d", (int)kinds[i]); printf(","); }
    printf("] got[");
    for (unsigned i = 0; i < n && expect_idx + i < ev_n; i++) {
        printf("%d", (int)ev_kind[expect_idx + i]); printf(",");
    }
    printf("%s\n", ok ? "] => PASS" : "] => FAIL");
    if (!ok) rc = 1;
    expect_idx += n;
}

static void report(void)
{
    static const unsigned up[]   = { BTN_PAIR_EV_STEP_UP };
    static const unsigned up_tog[] = { BTN_PAIR_EV_STEP_UP,
                                       BTN_PAIR_EV_FILTER_TOGGLE };
    printf("EVENTS total="); printf("%d\n", (int)ev_n);
    expect_seq("D1 bounce-press",   up,     1);
    expect_seq("D2 spike-nothing",  up,     0);
    expect_seq("D3 clean-press",    up,     1);
    expect_seq("D4 combo-bounce",   up_tog, 2);
    expect_seq("D5 early-release",  up,     1);
    if (ev_n != 5) { printf("TOTAL-COUNT FAIL got ");
                     printf("%d", (int)ev_n); printf(" want 5" "\n"); rc = 1; }
    printf("BTNDEGL-DONE rc=%d\n", (int)rc);
}

/* ---- the wrapper (mirrors production select loop) ------------------------ */
void btn_wrapper(void)
{
    btn_pair_fsm_t btn_pair;
    btn_pair_fsm_init(&btn_pair);

    /* production does this at task start (board.xc ~line 716); in xsim an
     * un-configured floating port SAMPLE GENERATES EVENTS and the parity
     * pinsneq cases storm. Model the real pad bias. */
    set_port_pull_down(p_butt_up);
    set_port_pull_down(p_butt_down);

    unsigned btn_up_val = 0, btn_dn_val = 0;
    unsigned btn_up_stable = 1, btn_dn_stable = 1;
    /* sim-only seam: the XS1 pad sample of these pins is not driven by the
     * stimulus (the inject case owns pad state), so the pinsneq cases are
     * runtime-disarmed to keep a single edge source. Production select has
     * no such guard -- there the pad IS the source. Everything else in the
     * select is verbatim production. */
    unsigned do_pads = 0;
    timer btn_up_tmr, btn_dn_tmr, btn_both_tmr, inj_tmr;
    unsigned btn_up_to = 0, btn_dn_to = 0;
    unsigned btn_both_to = 0, do_both = 0;
    unsigned inj_due = 0, do_inj = 1, inj_i = 0;

    unsigned t = MS(100);
    sched(t, 1);              /* D1 bounce press */
    sched(t + MS(5),   0);
    sched(t + MS(10),  1);
    sched(t + MS(120), 0);
    t += MS(300);
    sched(t, 1);              /* D2 spike: high 5..25 rel */
    sched(t + MS(20),  0);
    t += MS(300);
    sched(t, 1);              /* D3 clean press */
    sched(t + MS(60),  0);
    t += MS(300);
    sched(t, 1);              /* D4 combo w/ mid-hold bounce (absolute pad
                               * state: bit0 up, bit1 dn) */
    sched(t + MS(200),  3);   /* dn joins: combo entry (silent) */
    sched(t + MS(400),  2);   /* up bounces low, dn stays high */
    sched(t + MS(415),  3);   /* up settles high again (re-arm window) */
    sched(t + MS(2000), 0);   /* release both */
    t += MS(2600);
    sched(t, 1);              /* D5 early release */
    sched(t + MS(150),  3);   /* dn joins */
    sched(t + MS(950),  0);   /* release both before 1 s hold */

    unsigned t0; inj_tmr :> t0;
    inj_due = t0 + inj_t[0];

    while (1) {
        unsigned fired = 0;
        select {
            /* cold for parity: real pad path (mirrors production; the
             * do_pads guard is the sim-only single-edge-source seam) */
            case do_pads && btn_up_stable => p_butt_up when pinsneq(btn_up_val) :> btn_up_val:
                btn_up_stable = 0;
                btn_pair_fsm_edge(&btn_pair, BTN_PAIR_UP);
                btn_up_tmr :> btn_up_to;
                btn_up_to += BTN_DEBOUNCE_TICKS;
                fired = 1;
                break;

            case !btn_up_stable => btn_up_tmr when timerafter(btn_up_to) :> void:
                btn_up_stable = 1;
                btn_up_val = pad_up;                        /* RE-SAMPLE pad */
                printf("WIN up pad=%d fsmup=%d fsmdn=%d\n", (int)pad_up,
                       (int)btn_pair.up_val, (int)btn_pair.dn_val);
                {
                    unsigned ev = btn_pair_fsm_stable(&btn_pair, BTN_PAIR_UP,
                                                      btn_up_val);
                    if (ev == BTN_PAIR_EV_STEP_UP) log_ev(ev);
                    if (btn_pair.hold_armed && !do_both) {
                        do_both = 1;
                        btn_both_tmr :> btn_both_to;
                        btn_both_to += BTN_HOLD_TICKS;
                        printf("ARM hold due=%u\n", btn_both_to);
                    }
                }
                fired = 1;
                break;

            case do_pads && btn_dn_stable => p_butt_down when pinsneq(btn_dn_val) :> btn_dn_val:
                btn_dn_stable = 0;
                btn_pair_fsm_edge(&btn_pair, BTN_PAIR_DOWN);
                btn_dn_tmr :> btn_dn_to;
                btn_dn_to += BTN_DEBOUNCE_TICKS;
                fired = 1;
                break;

            case !btn_dn_stable => btn_dn_tmr when timerafter(btn_dn_to) :> void:
                btn_dn_stable = 1;
                btn_dn_val = pad_dn;                        /* RE-SAMPLE pad */
                printf("WIN dn pad=%d fsmup=%d fsmdn=%d\n", (int)pad_dn,
                       (int)btn_pair.up_val, (int)btn_pair.dn_val);
                {
                    unsigned ev = btn_pair_fsm_stable(&btn_pair, BTN_PAIR_DOWN,
                                                      btn_dn_val);
                    if (ev == BTN_PAIR_EV_STEP_DOWN) log_ev(ev);
                    if (btn_pair.hold_armed && !do_both) {
                        do_both = 1;
                        btn_both_tmr :> btn_both_to;
                        btn_both_to += BTN_HOLD_TICKS;
                        printf("ARM hold due=%u\n", btn_both_to);
                    }
                }
                fired = 1;
                break;

            case do_both => btn_both_tmr when timerafter(btn_both_to) :> void:
                do_both = 0;
                printf("HOLD-CASE armed=%d ufired=%d\n", (int)btn_pair.hold_armed,
                       (int)btn_pair.hold_fired);
                if (btn_pair_fsm_hold_expired(&btn_pair) == BTN_PAIR_EV_FILTER_TOGGLE)
                    log_ev(BTN_PAIR_EV_FILTER_TOGGLE);
                fired = 1;
                break;

            /* stimulus: run the same statements the pinsneq cases run */
            case do_inj => inj_tmr when timerafter(inj_due) :> void: {
                unsigned m = inj_mask[inj_i];
                unsigned now; inj_tmr :> now;
                pad_up = m & 1u;
                pad_dn = (m >> 1) & 1u;
                printf("INJ %d u=%d d=%d\n", (int)inj_i, (int)pad_up, (int)pad_dn);
                if (pad_up != btn_up_val) {
                    btn_up_stable = 0;
                    btn_pair_fsm_edge(&btn_pair, BTN_PAIR_UP);
                    btn_up_tmr :> btn_up_to;
                    btn_up_to += BTN_DEBOUNCE_TICKS;
                    btn_up_val = pad_up;   /* pinsneq would latch the value */
                }
                if (pad_dn != btn_dn_val) {
                    btn_dn_stable = 0;
                    btn_pair_fsm_edge(&btn_pair, BTN_PAIR_DOWN);
                    btn_dn_tmr :> btn_dn_to;
                    btn_dn_to += BTN_DEBOUNCE_TICKS;
                    btn_dn_val = pad_dn;
                }
                inj_i++;
                if (inj_i < inj_n) inj_due = now + (inj_t[inj_i] - inj_t[inj_i - 1]);
                else               do_inj = 0;
                fired = 1;
            } break;
        }
        (void) fired;
        /* done when stimulus is exhausted AND both windows settled */
        if (inj_i >= inj_n && !do_both && btn_up_stable && btn_dn_stable) break;
    }

    report();
    _Exit(rc);
}

int main(void)
{
    par {
        on tile[0]: btn_wrapper();
    }
    return 0;
}
