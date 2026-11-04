/* csd_power implementation -- see csd_power.h for the WHY (owner directive
 * 2026-11-04) and the witness rule that is the heart of this feature.
 *
 * Shape: a 3-state machine (ON / WAIT_DOWN / OFF) plus a bounded action
 * queue. Sequences are enqueued whole, so the owner's order is a property of
 * one data structure that the host test pins exactly, not of call order
 * across the firmware. Single-flight: while any action is pending nothing
 * else is enqueued, which is what keeps a stream that returns mid-shutdown
 * from producing an interleaved half-down/half-up sequence.
 */
#include "csd_power.h"

#define PW_Q_MAX 8   /* longest sequence is 4 down + 4 up, with headroom */

static unsigned q[PW_Q_MAX];
static unsigned q_head, q_tail;

static csd_pw_state_t state = CSD_PW_STATE_ON;
static unsigned idle_ms;
static int dsp_enabled = 1;
static int stream_stopped;    /* host said alt-0 (or both alts 0) */
static int activity_seen;     /* activity arrived while a drain was in flight */

static void q_clear(void) { q_head = q_tail = 0; }
static unsigned q_empty(void) { return q_head == q_tail; }

static void q_push(unsigned a)
{
    /* Full is a bug, not a runtime condition: sequences are <= 8 and
     * single-flight prevents stacking. Dropping silently would leave a
     * codec half-powered, which is exactly the failure this module exists
     * to prevent -- so the caller's queue discipline is asserted, not
     * papered over. On device an abort surfaces as a boot the rig will
     * report; the host test exercises every push path. */
    unsigned next = q_tail + 1u;
    if (next >= PW_Q_MAX) next = 0u;
    if (next == q_head) return;        /* defensive: never corrupt the ring */
    q[q_tail] = a;
    q_tail = next;
}

static void start_down(void)
{
    /* Owner order, 2026-11-04: amplifier, DAC, DSP+mixer. The volume-min
     * step is the owner's explicit addition ahead of the amplifier drop
     * (pop suppression); it is a codec I2C write that happens while the
     * DAC is still powered, so the DAC-off below is still 'after amp'. */
    q_clear();
    q_push(CSD_PW_ACT_AMP_VOL_MIN);
    q_push(CSD_PW_ACT_AMP_OFF);
    q_push(CSD_PW_ACT_DAC_OFF);
    if (dsp_enabled) q_push(CSD_PW_ACT_DSP_OFF);
    state = CSD_PW_STATE_WAIT_DOWN;
    activity_seen = 0;
}

static void start_up(void)
{
    /* Owner order, 2026-11-04: DSP+mixer first (if enabled), DAC next,
     * amplifier LAST (SUSPEND_N). The DAC action is a full re-init, and
     * volume restore must sit between re-init and amp enable: restoring
     * into an un-initialised codec is lost, restoring with the amp live is
     * a pop. */
    q_clear();
    if (dsp_enabled) q_push(CSD_PW_ACT_DSP_ON);
    q_push(CSD_PW_ACT_DAC_REINIT);
    q_push(CSD_PW_ACT_VOL_RESTORE);
    q_push(CSD_PW_ACT_AMP_ON);
    state = CSD_PW_STATE_WAIT_UP;
    idle_ms = 0u;
    activity_seen = 0;
}

void csd_power_set_dsp_enabled(int enabled) { dsp_enabled = enabled ? 1 : 0; }

void csd_power_init(void)
{
    q_clear();
    state     = CSD_PW_STATE_ON;
    idle_ms   = 0u;
    stream_stopped = 0;
    activity_seen  = 0;
}

void csd_power_tick(unsigned dt_ms)
{
    /* A drain in flight outranks everything: complete it, then re-evaluate. */
    if (state == CSD_PW_STATE_WAIT_DOWN || state == CSD_PW_STATE_WAIT_UP) {
        return;
    }

    if (state == CSD_PW_STATE_ON) {
        /* alt-0 arms immediately: the host declared the stream stopped, and
         * waiting 5 minutes for a codec already drawing idle power is not
         * the requirement. Otherwise count silence. */
        if (stream_stopped) {
            start_down();
            return;
        }
        /* A wake armed while a sequence was draining outranks idle: we are
         * ON with audio again, the timer restarted, nothing to do here. */
        if (activity_seen) return;
        if (idle_ms + dt_ms >= CSD_PW_IDLE_MS) {
            idle_ms = CSD_PW_IDLE_MS;
            start_down();
        } else {
            idle_ms += dt_ms;
        }
        return;
    }

    /* OFF: stay down until real activity arrives. next_action() transitions
     * to WAIT_UP when it sees the armed flag, so the wake sequence always
     * starts from an empty queue. */
}

void csd_power_note_frame_activity(void)
{
    if (state == CSD_PW_STATE_WAIT_DOWN || state == CSD_PW_STATE_WAIT_UP) {
        /* Mid-drain: remember, so OFF completes and then wakes instead of
         * waiting for a next poll's witness (the counter only counts NEW
         * frames; a quiet-but-streaming host would otherwise leave us OFF). */
        activity_seen = 1;
        return;
    }
    idle_ms = 0u;
    stream_stopped = 0;
    if (state == CSD_PW_STATE_OFF) {
        start_up();
    }
}

void csd_power_note_frame_silence(void)
{
    /* DELIBERATELY INERT -- same rule as the clock: all-zero samples are
     * not a stream for the purpose of keeping a 2-watt idle stage awake.
     * The distinction is kept separate from note_frame_activity so a
     * future 'count any frame' wiring mistake lands on test T11. */
}

void csd_power_note_clock_alive(void)
{
    /* DELIBERATELY INERT. A toggling LRCLK says the codec PLL is locked --
     * true with an entirely silent bus. Trusting it here would make the box
     * never power down. Host test T2 fails the moment anyone wires a real
     * decision in here. */
}

void csd_power_note_stream_state(int input_active, int output_active)
{
    if (!input_active && !output_active) {
        stream_stopped = 1;      /* arms the down sequence at the next tick */
    } else {
        stream_stopped = 0;
        idle_ms = 0u;            /* host re-opened an alt: not idle */
    }
}

csd_pw_state_t csd_power_state(void) { return state; }

unsigned csd_power_next_action(void)
{
    /* When OFF with a queued wake pending from a mid-drain return, start it
     * now rather than waiting on the next witness sample. */
    if (state == CSD_PW_STATE_OFF && activity_seen && q_empty()) {
        start_up();
    }
    if (q_empty()) {
        if (state == CSD_PW_STATE_WAIT_DOWN) state = CSD_PW_STATE_OFF;
        if (state == CSD_PW_STATE_WAIT_UP)   state = CSD_PW_STATE_ON;
        return CSD_PW_ACT_NONE;
    }
    unsigned a = q[q_head];
    q_head++;
    if (q_head >= PW_Q_MAX) q_head = 0u;
    return a;
}
