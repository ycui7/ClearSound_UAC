/*
 * btn_pair_fsm_test.xc -- scenario harness for button_pair_fsm.h.
 *
 * Runs on the host (gcc -DHOST_TEST) and in xsim (xcc -target=xs3a).
 * It replicates the AudioHwRemoteTile0 select-loop CONTRACT so the wrapper
 * semantics are exercised, not only the decision table:
 *
 *   edge(which)          : pinsneq case        -> btn_pair_fsm_edge()
 *   window expiry        : debounce timer case -> RE-SAMPLE pad, then
 *                                             btn_pair_fsm_stable(level)
 *   hold arming          : after any fsm call  -> if (hold_armed && !do_both)
 *                                                  do_both=1, arm 1 s timer
 *   hold expiry          : hold timer case     -> btn_pair_fsm_hold_expired()
 *
 * The simulated pad (pad_up/pad_dn) is what a re-sample would return; a
 * "spike" scenario flips the pad back low before the window expiry to prove
 * the re-sample behaviour (the old code acted on the edge-latched value and
 * could phantom-step on sub-window spikes).
 *
 * Exit evidence: one line per scenario (PASS/FAIL + details) and a final
 *   BTNTEST-DONE rc=<n>
 * line; rc counts FAILed scenarios. The xsim wrapper polls for that line.
 *
 *  COPYRIGHT (C) SPECIALTYCIRCUITS LLC 2026
 */
#ifndef HOST_TEST
#include <platform.h>
#include <xs1.h>
#endif
#include <stdio.h>
#include <stdlib.h>
#include "button_pair_fsm.h"

/* ---- wrapper model ------------------------------------------------------ */
typedef struct {
    btn_pair_fsm_t fsm;
    unsigned pad_up, pad_dn;        /* what a pad re-sample would return   */
    unsigned do_both;               /* hold timer running                  */
} tw_t;

static void tw_init(tw_t *w)
{
    btn_pair_fsm_init(&w->fsm);
    w->pad_up = 0u; w->pad_dn = 0u; w->do_both = 0u;
}

/* port edge observed (select pinsneq case) */
static void tw_edge(tw_t *w, unsigned which)
{
    btn_pair_fsm_edge(&w->fsm, which);
}

/* debounce window expired: RE-SAMPLE the pad and decide */
static unsigned tw_window(tw_t *w, unsigned which)
{
    unsigned level = (which == BTN_PAIR_UP) ? w->pad_up : w->pad_dn;
    unsigned ev = btn_pair_fsm_stable(&w->fsm, which, level);
    if (w->fsm.hold_armed && !w->do_both) w->do_both = 1u;   /* arm 1 s */
    return ev;
}

/* hold timer expired */
static unsigned tw_hold(tw_t *w)
{
    unsigned ev = btn_pair_fsm_hold_expired(&w->fsm);
    w->do_both = 0u;
    return ev;
}

/* press gesture: pad goes high, edge fires, window expires at high */
static unsigned tw_press(tw_t *w, unsigned which)
{
    if (which == BTN_PAIR_UP) w->pad_up = 1u; else w->pad_dn = 1u;
    tw_edge(w, which);
    return tw_window(w, which);
}
/* release gesture */
static unsigned tw_release(tw_t *w, unsigned which)
{
    if (which == BTN_PAIR_UP) w->pad_up = 0u; else w->pad_dn = 0u;
    tw_edge(w, which);
    return tw_window(w, which);
}

/* ---- assertions --------------------------------------------------------- */
static int failures = 0;
static int checks_in_scenario;
static int failed_in_scenario;

static void expect(const char *sc, unsigned got, unsigned want, const char *what)
{
    checks_in_scenario++;
    if (got != want) {
        failed_in_scenario++;
        printf("FAIL %s: %s got %u want %u\n", sc, what, got, want);
    }
}

static void scenario_end(const char *sc)
{
    if (failed_in_scenario) failures++;
    printf("%s %s (%d checks)\n", failed_in_scenario ? "FAIL" : "PASS",
           sc, checks_in_scenario);
    checks_in_scenario = failed_in_scenario = 0;
}

/* ---- scenarios ---------------------------------------------------------- */
static void s1_single_press(void)
{
    tw_t w; tw_init(&w);
    const char *sc = "S1 single press steps once";
    expect(sc, tw_press(&w, BTN_PAIR_UP), BTN_PAIR_EV_STEP_UP, "up press");
    expect(sc, tw_release(&w, BTN_PAIR_UP), BTN_PAIR_EV_NONE, "up release");
    /* held again then window without a new press must not re-fire */
    expect(sc, tw_press(&w, BTN_PAIR_DOWN), BTN_PAIR_EV_STEP_DOWN, "dn press");
    expect(sc, tw_release(&w, BTN_PAIR_DOWN), BTN_PAIR_EV_NONE, "dn release");
    scenario_end(sc);
}

static void s2_spike_below_window(void)
{
    tw_t w; tw_init(&w);
    const char *sc = "S2 spike <window ignored (re-sample)";
    w.pad_up = 1u;                 /* transient spike */
    tw_edge(&w, BTN_PAIR_UP);      /* edge latched 1 in real task... */
    w.pad_up = 0u;                 /* ...pad is LOW again before expiry */
    expect(sc, tw_window(&w, BTN_PAIR_UP), BTN_PAIR_EV_NONE, "spike no step");
    expect(sc, w.fsm.up_val, 0u, "state clean");
    scenario_end(sc);
}

static void s3_combo_released_early(void)
{
    tw_t w; tw_init(&w);
    const char *sc = "S3 combo <1 s: no toggle";
    expect(sc, tw_press(&w, BTN_PAIR_UP), BTN_PAIR_EV_STEP_UP, "1st press steps");
    expect(sc, tw_press(&w, BTN_PAIR_DOWN), BTN_PAIR_EV_NONE, "2nd press silent");
    expect(sc, w.do_both, 1u, "hold armed");
    expect(sc, tw_release(&w, BTN_PAIR_UP), BTN_PAIR_EV_NONE, "release up");
    expect(sc, tw_release(&w, BTN_PAIR_DOWN), BTN_PAIR_EV_NONE, "release dn");
    expect(sc, tw_hold(&w), BTN_PAIR_EV_NONE, "hold expired after release");
    scenario_end(sc);
}

static void s4_combo_hold_once(void)
{
    tw_t w; tw_init(&w);
    const char *sc = "S4 hold 1 s: exactly one toggle";
    (void) tw_press(&w, BTN_PAIR_UP);
    (void) tw_press(&w, BTN_PAIR_DOWN);
    expect(sc, tw_hold(&w), BTN_PAIR_EV_FILTER_TOGGLE, "first expiry toggles");
    expect(sc, tw_hold(&w), BTN_PAIR_EV_NONE, "second expiry silent (I3)");
    /* keep holding 3 more simulated seconds: no further events */
    expect(sc, tw_hold(&w), BTN_PAIR_EV_NONE, "still holding");
    scenario_end(sc);
}

static void s5_rearm_after_full_release(void)
{
    tw_t w; tw_init(&w);
    const char *sc = "S5 new combo after full release toggles again";
    (void) tw_press(&w, BTN_PAIR_UP);
    (void) tw_press(&w, BTN_PAIR_DOWN);
    expect(sc, tw_hold(&w), BTN_PAIR_EV_FILTER_TOGGLE, "first toggle");
    (void) tw_release(&w, BTN_PAIR_UP);
    (void) tw_release(&w, BTN_PAIR_DOWN);
    (void) tw_press(&w, BTN_PAIR_UP);
    (void) tw_press(&w, BTN_PAIR_DOWN);
    expect(sc, tw_hold(&w), BTN_PAIR_EV_FILTER_TOGGLE, "second toggle");
    scenario_end(sc);
}

static void s6_bounce_during_hold(void)
{
    tw_t w; tw_init(&w);
    const char *sc = "S6 bounce during hold: no fire while unstable, re-arm";
    (void) tw_press(&w, BTN_PAIR_UP);
    (void) tw_press(&w, BTN_PAIR_DOWN);
    /* bounce on up: edge drops stable, settles back pressed */
    tw_edge(&w, BTN_PAIR_UP);                  /* unstable now */
    expect(sc, tw_hold(&w), BTN_PAIR_EV_NONE, "no fire while unstable (I4)");
    (void) tw_window(&w, BTN_PAIR_UP);         /* settles pressed, re-arms */
    expect(sc, w.do_both, 1u, "re-armed");
    expect(sc, tw_hold(&w), BTN_PAIR_EV_FILTER_TOGGLE, "toggles after settle");
    scenario_end(sc);
}

static void s7_single_after_combo(void)
{
    tw_t w; tw_init(&w);
    const char *sc = "S7 single press works again after combo";
    (void) tw_press(&w, BTN_PAIR_UP);
    (void) tw_press(&w, BTN_PAIR_DOWN);
    expect(sc, tw_hold(&w), BTN_PAIR_EV_FILTER_TOGGLE, "toggle");
    (void) tw_release(&w, BTN_PAIR_DOWN);
    (void) tw_release(&w, BTN_PAIR_UP);        /* fully clear: combo ends */
    expect(sc, tw_press(&w, BTN_PAIR_DOWN), BTN_PAIR_EV_STEP_DOWN, "step again");
    scenario_end(sc);
}

static void s8_partial_release_no_rearm(void)
{
    tw_t w; tw_init(&w);
    const char *sc = "S8 half-release does not end the combo";
    (void) tw_press(&w, BTN_PAIR_UP);
    (void) tw_press(&w, BTN_PAIR_DOWN);
    expect(sc, tw_hold(&w), BTN_PAIR_EV_FILTER_TOGGLE, "toggle fired");
    (void) tw_release(&w, BTN_PAIR_DOWN);      /* up still held: fired stays */
    (void) tw_press(&w, BTN_PAIR_DOWN);        /* press both again, up never
                                                * released: same combo */
    expect(sc, tw_hold(&w), BTN_PAIR_EV_NONE, "no second toggle (I3)");
    scenario_end(sc);
}

static void all_tests(void)
{
    s1_single_press();
    s2_spike_below_window();
    s3_combo_released_early();
    s4_combo_hold_once();
    s5_rearm_after_full_release();
    s6_bounce_during_hold();
    s7_single_after_combo();
    s8_partial_release_no_rearm();
    printf("BTNTEST-DONE rc=%d\n", failures);
#ifdef HOST_TEST
    exit(failures ? 1 : 0);
#endif
}

#ifndef HOST_TEST
int main(void)
{
    all_tests();
    while (1) { }               /* wrapper kills after the DONE line */
    return 0;
}
#else
int main(void) { all_tests(); return failures ? 1 : 0; }
#endif
