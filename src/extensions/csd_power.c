/* csd_power implementation -- see csd_power.h for the WHY (owner directive
 * 2026-11-04) and for the reason the action queue was deleted (owner,
 * 2026-11-05: "just a well designed and tested sequence of actions to disable
 * amp and re-enable amp").
 *
 * Two states, two sequences, and each sequence runs straight through. That is
 * the whole policy. Everything that can hurt -- the order, the volume floor
 * before the amplifier drops, the full re-init instead of an un-mute, the
 * amplifier last -- is a property of one straight-line function that the host
 * test records and asserts, instead of control flow spread across a poll loop
 * and a ring buffer.
 */
#include "csd_power.h"

/* Whole feature is compile-gated: without the flag this TU contributes no
 * code, so a build that does not ask for idle power is unchanged. The host
 * test compiles with -DCSD_IDLE_POWER. */
#ifdef CSD_IDLE_POWER

static csd_pw_state_t state = CSD_PW_STATE_ON;
static unsigned idle_ms;
static int dsp_enabled = 1;

/* Owner's case-14 semantics live here, as one signed number:
 *   < 0  never sleep        0  sleep now       > 0  seconds
 * Keeping it a single int (not a flag plus a timer) means there is exactly one
 * place that decides whether the box may sleep, and the host test drives all
 * three regimes through it. Default is the owner's 300 s. */
static int timeout_sec = (int)(CSD_PW_IDLE_MS / 1000u);

/* csd_pw_execute() and csd_pw_audio_arrived() are the platform's, declared in
 * the header; the host test links its own recorder and script. */
static void perform(unsigned a) { csd_pw_execute(a); }
static int  audio_arrived(void) { return csd_pw_audio_arrived() != 0u; }

/* DOWN -- owner order 2026-11-04: amplifier, DAC, DSP+mixer. The volume floor
 * is the owner's explicit addition ahead of the amplifier drop (pop
 * suppression); it is a codec I2C write issued while the DAC is still powered,
 * so the DAC-off below is still 'after the amp'.
 *
 * Runs to completion. The actuator commands are one-way (assistant -> codec
 * server) over a dedicated chanend, and the server executes each before
 * returning to its select, so they arrive and execute in this order with no
 * handshake to time out and no half-applied state to recover from. */
static void sequence_down(void)
{
    perform(CSD_PW_ACT_AMP_VOL_MIN);
    perform(CSD_PW_ACT_AMP_OFF);
    perform(CSD_PW_ACT_DAC_OFF);
    if (dsp_enabled) perform(CSD_PW_ACT_DSP_OFF);
    state   = CSD_PW_STATE_OFF;
    idle_ms = 0u;
}

/* UP -- owner order 2026-11-04: DSP+mixer first (if enabled), DAC next,
 * amplifier LAST (SUSPEND_N). The DAC step is a FULL boot re-init, not an
 * un-mute, and the volume restore sits between the re-init and the amplifier:
 * restoring into an un-initialised codec is lost, and restoring with the amp
 * live is a pop. */
static void sequence_up(void)
{
    if (dsp_enabled) perform(CSD_PW_ACT_DSP_ON);
    perform(CSD_PW_ACT_DAC_REINIT);
    perform(CSD_PW_ACT_VOL_RESTORE);
    perform(CSD_PW_ACT_AMP_ON);
    state   = CSD_PW_STATE_ON;
    /* Measured from when the box is audible again, not from when the wake was
     * requested: a wake that spent time in PLL settle must not count that time
     * as idle. */
    idle_ms = 0u;
}

void csd_power_set_dsp_enabled(int enabled)     { dsp_enabled = enabled ? 1 : 0; }
void csd_power_init(void)                       { state = CSD_PW_STATE_ON; idle_ms = 0u; }

void csd_power_set_timeout_sec(int sec)
{
    timeout_sec = sec;
    /* Measure from the moment the owner asked, not from the last sample: a set
     * of 0 must fire on the next tick, and a set of 30 must mean 30 s from now
     * rather than 30 s minus whatever had already elapsed. */
    idle_ms = 0u;
}

int  csd_power_get_timeout_sec(void)            { return timeout_sec; }

/* Boot-only: adopt the persisted value without resetting the idle clock, so a
 * restore that lands after the first tick cannot silently extend the window. */
void csd_power_timeout_restore(int sec)         { timeout_sec = sec; }

void csd_power_poll(unsigned dt_ms)
{
    /* No executor guard needed: csd_pw_execute() is a plain extern, so a
     * build that forgot to provide one fails at link time instead of running
     * silently with every counter moving and no actuator ever moving. That is
     * the advantage of externs over an injected struct -- the failure the
     * pointer version had to check for at runtime, the linker checks for us. */

    if (state == CSD_PW_STATE_ON) {
        /* Negative asks "never sleep" (owner case 14). Checked FIRST and
         * returned from before the witness is polled: csd_pw_audio_arrived()
         * is edge-consuming by contract, and draining it while the box has no
         * intention of sleeping would throw away the very edge that a later
         * positive setting needs to wake on. */
        if (timeout_sec < 0) return;

        /* Sparse real audio holds the box up indefinitely: any genuine sample
         * restarts the clock (host test T4). All-zero frames and a live LRCLK
         * never count -- the witness that decides this excludes the buffer
         * path's underfill word, and was measured frozen (act 868140) while
         * the frame counter climbed past 12 M in the same window. */
        if (audio_arrived()) { idle_ms = 0u; return; }

        /* timeout_sec == 0 makes the limit 0, so the very next tick tears the
         * chain down: "immediately", without this function ever running a
         * sequence outside its single owning task. */
        unsigned limit_ms = (unsigned)timeout_sec * 1000u;

        if (idle_ms + dt_ms >= limit_ms) {
            sequence_down();
            /* Audio may have returned WHILE the teardown executed -- the
             * channel sends and the codec re-init both block. Honour it now:
             * after the chain is fully down, never in the middle of it. This
             * is the case the deleted queue existed for, and the reason it did
             * not need one: a sequence cannot be interrupted, so "complete the
             * shutdown, then wake" is just the next line of code. */
            if (audio_arrived()) sequence_up();
        } else {
            idle_ms += dt_ms;
        }
        return;
    }

    /* OFF: stay down until real activity arrives. */
    if (audio_arrived()) sequence_up();
}

/* Inside the guard: csd_power.c is globbed into every build by
 * XMOS_REGISTER_APP, so getters at the file tail referenced undeclared
 * statics and broke the feature-free release build (measured 2026-11-05). */
csd_pw_state_t csd_power_state(void) { return state; }
unsigned csd_power_idle_ms_dbg(void) { return idle_ms; }

#endif /* CSD_IDLE_POWER */

/* Empty TU is not strictly legal C; this keeps the file well-formed when the
 * feature is compiled out. */
typedef int csd_pw_unused_t;
