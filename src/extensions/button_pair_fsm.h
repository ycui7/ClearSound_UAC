/*
 * button_pair_fsm.h -- pure logic for the two-button hold feature.
 *
 * Feature (owner request 2026-11-16): holding BOTH volume buttons for more
 * than 1 s toggles the ES9219Q 8x FIR between linear phase fast roll-off
 * (reg 7 [7:5] = 3'b000) and hybrid fast roll-off (3'b110). A single button
 * press keeps its +1/-1 dB step.
 *
 * This header holds the DECISION logic only: no ports, no timers, no I2C.
 * The board task (AudioHwRemoteTile0) owns the ports, the three timers and
 * the side effects (volume step, reg-7 write). That split is deliberate:
 * the decision table is testable in xsim and on the host, the wiring is
 * testable on the bench. The companion test harness
 * tools/btnxsim/btn_pair_fsm_test.xc replicates the task's select-loop
 * CONTRACT (edge -> 50 ms window -> re-sample -> on_stable; arm -> hold
 * timeout -> hold_expired), so a wrapper regression that violates the
 * contract shows up as a scenario FAIL there.
 *
 * Invariants (asserted by the harness):
 *  I1  A confirmed single-button press emits exactly one step event.
 *  I2  A press confirmed while the OTHER button is held emits NO step event
 *      (combo entry is silent) and arms the hold timer.
 *  I3  The hold timer emits TOGGLE at most once per combo; "combo" ends only
 *      when BOTH buttons are stably released.
 *  I4  Nothing fires while either button is bouncing (stable flag low).
 *  I5  Release edges never emit events.
 *
 *  COPYRIGHT (C) SPECIALTYCIRCUITS LLC 2026
 */
#ifndef BUTTON_PAIR_FSM_H_
#define BUTTON_PAIR_FSM_H_

#define BTN_PAIR_EV_NONE          0u
#define BTN_PAIR_EV_STEP_UP       1u
#define BTN_PAIR_EV_STEP_DOWN     2u
#define BTN_PAIR_EV_FILTER_TOGGLE 4u

#define BTN_PAIR_UP   0u
#define BTN_PAIR_DOWN 1u

typedef struct btn_pair_fsm_s {
    unsigned up_val;       /* last confirmed level, 1 = pressed        */
    unsigned dn_val;
    unsigned up_stable;    /* 1 = port settled, 0 = inside debounce    */
    unsigned dn_stable;
    unsigned hold_armed;   /* combo condition seen; task runs the timer */
    unsigned hold_fired;   /* TOGGLE already emitted for this combo     */
} btn_pair_fsm_t;

static inline void btn_pair_fsm_init(btn_pair_fsm_t *s)
{
    s->up_val = 0u; s->dn_val = 0u;
    s->up_stable = 1u; s->dn_stable = 1u;
    s->hold_armed = 0u; s->hold_fired = 0u;
}

/* A port edge invalidated the stable window for one button. The task calls
 * this from its pinsneq case; nothing is decided here (I4). */
static inline void btn_pair_fsm_edge(btn_pair_fsm_t *s, unsigned which)
{
    if (which == BTN_PAIR_UP) s->up_stable = 0u;
    else                      s->dn_stable = 0u;
}

/* The task's debounce window EXPIRED for one button and re-sampled the pad:
 * 'level' is the value now on the pin (not the value latched at the edge --
 * the 2026-11-16 fix against sub-window spikes). Returns the event to act on. */
static inline unsigned btn_pair_fsm_stable(btn_pair_fsm_t *s,
                                           unsigned which, unsigned level)
{
    unsigned ev = BTN_PAIR_EV_NONE;
    unsigned other;

    if (which == BTN_PAIR_UP) { s->up_val = level; s->up_stable = 1u; }
    else                      { s->dn_val = level; s->dn_stable = 1u; }

    if (level == 0u) {                       /* confirmed release: I5 */
        s->hold_armed = 0u;
        if (s->up_val == 0u && s->dn_val == 0u)
            s->hold_fired = 0u;              /* combo ends only fully clear */
        return ev;
    }

    other = (which == BTN_PAIR_UP) ? s->dn_val : s->up_val;
    if (other != 0u) {
        s->hold_armed = 1u;                  /* I2: silent combo entry */
    } else {
        ev = (which == BTN_PAIR_UP) ? BTN_PAIR_EV_STEP_UP
                                    : BTN_PAIR_EV_STEP_DOWN;   /* I1 */
    }
    return ev;
}

/* The task's 1 s hold timer expired. Emits the TOGGLE when the combo is
 * still genuinely held (both pressed AND both settled, I4) and has not
 * fired yet (I3). The task arms the timer whenever hold_armed rises from 0. */
static inline unsigned btn_pair_fsm_hold_expired(btn_pair_fsm_t *s)
{
    if (s->hold_armed == 0u) return BTN_PAIR_EV_NONE;
    s->hold_armed = 0u;
    if (s->hold_fired == 0u &&
        s->up_val && s->dn_val && s->up_stable && s->dn_stable) {
        s->hold_fired = 1u;
        return BTN_PAIR_EV_FILTER_TOGGLE;
    }
    return BTN_PAIR_EV_NONE;
}

#endif /* BUTTON_PAIR_FSM_H_ */
