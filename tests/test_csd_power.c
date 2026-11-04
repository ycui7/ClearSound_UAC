/* Host unit test for csd_power.c -- ordered idle-power state machine
 * (TODO item 7, owner directive 2026-11-04). Written BEFORE the
 * implementation (ub-mode): each assertion is a contract the firmware
 * wiring relies on, so green is evidence, not decoration.
 *
 *   gcc -O2 -I src/extensions tests/test_csd_power.c \
 *       src/extensions/csd_power.c -o /tmp/tpower && /tmp/tpower
 *
 * THE WITNESS RULE (the burned-week lesson): a free-running I2S clock proves
 * NOTHING about stream content -- the codec DPLL locks on clock alone with no
 * samples on the bus. 'No stream' is therefore defined ONLY by:
 *   (a) frame-content activity  -- a non-zero DAC-bound frame in this window,
 *   (b) an explicit stream-stop event (SET_INTERFACE alt 0).
 * Never the LRCLK, never "the port is clocking". T2 exists to prove a
 * clock-only build cannot pass.
 *
 * ORDER IS THE SPEC. Down: amp, DAC, DSP+mixer. Up: DSP+mixer, DAC, amp
 * (SUSPEND_N last). Hard requirements: DAC wake is a FULL re-init with prior
 * volume restored, and amp-down is preceded by volume-to-minimum.
 */
#include <stdio.h>
#include <stdint.h>
#include "csd_power.h"

static int fails = 0, checks = 0;
#define CK(cond, msg) do { checks++; if(!(cond)){ fails++; \
    printf("FAIL: %s  (line %d)\n", msg, __LINE__);} } while(0)

static int drain(unsigned *buf, int cap)
{
    int n = 0;
    while (n < cap) {
        unsigned a = csd_power_next_action();
        if (a == CSD_PW_ACT_NONE) break;
        buf[n++] = a;
    }
    return n;
}

static void reset_build(int dsp_enabled)
{
    csd_power_set_dsp_enabled(dsp_enabled);
    csd_power_init();
}

static void expect_seq(const unsigned *want, int n, const char *what)
{
    unsigned got[16];
    int gn = drain(got, 16);
    checks++;
    if (gn != n) {
        fails++;
        printf("FAIL: %s: got %d actions, want %d:", what, gn, n);
        for (int i = 0; i < gn; i++) printf(" %u", got[i]);
        printf("\n");
        return;
    }
    for (int i = 0; i < n; i++) {
        checks++;
        if (got[i] != want[i]) {
            fails++;
            printf("FAIL: %s: action[%d]=%u want %u\n", what, i, got[i], want[i]);
            return;
        }
    }
}

static void tick(unsigned n) { for (unsigned i = 0; i < n; i++) csd_power_tick(1u); }

int main(void)
{
    unsigned s[16];
    const unsigned DOWN_4[] = {CSD_PW_ACT_AMP_VOL_MIN, CSD_PW_ACT_AMP_OFF,
                               CSD_PW_ACT_DAC_OFF,     CSD_PW_ACT_DSP_OFF};
    const unsigned DOWN_3[] = {CSD_PW_ACT_AMP_VOL_MIN, CSD_PW_ACT_AMP_OFF,
                               CSD_PW_ACT_DAC_OFF};
    const unsigned UP_4[]   = {CSD_PW_ACT_DSP_ON,      CSD_PW_ACT_DAC_REINIT,
                               CSD_PW_ACT_VOL_RESTORE, CSD_PW_ACT_AMP_ON};

    /* T1: boot is fully ON, nothing pending. */
    reset_build(1);
    CK(csd_power_state() == CSD_PW_STATE_ON, "T1 boot state ON");
    CK(csd_power_next_action() == CSD_PW_ACT_NONE, "T1 no pending action");

    /* T2: THE BURNED-WEEK RULE. A live I2S clock is not activity: a build
     * that trusted the clock would never time out and would burn idle power
     * forever. Clock-alive must be inert. */
    reset_build(1);
    for (unsigned i = 0; i < CSD_PW_IDLE_MS + 10u; i++) csd_power_note_clock_alive();
    tick(CSD_PW_IDLE_MS + 10u);
    expect_seq(DOWN_4, 4, "T2 clock-alone still times out (clock is NOT a witness)");

    /* T3: the 5-minute bound is exact and strict. */
    reset_build(1);
    tick(CSD_PW_IDLE_MS - 1u);
    CK(csd_power_state() == CSD_PW_STATE_ON, "T3 under 5 min: still ON");
    CK(csd_power_next_action() == CSD_PW_ACT_NONE, "T3 no action early");
    tick(1u);
    CK(csd_power_state() == CSD_PW_STATE_WAIT_DOWN, "T3 5-min boundary arms down");

    /* T4: real frame activity holds the box awake indefinitely. */
    reset_build(1);
    for (unsigned r = 0; r < 10u; r++) {
        tick(CSD_PW_IDLE_MS - 1000u);
        csd_power_note_frame_activity();
        tick(999u);
    }
    CK(csd_power_state() == CSD_PW_STATE_ON, "T4 sparse real audio never powers down");

    /* T5: shutdown ORDER is the spec (dsp built in). */
    reset_build(1);
    tick(CSD_PW_IDLE_MS + 1u);
    expect_seq(DOWN_4, 4, "T5 shutdown order (dsp enabled)");
    CK(csd_power_state() == CSD_PW_STATE_OFF, "T5 reaches OFF after drain");
    CK(csd_power_next_action() == CSD_PW_ACT_NONE, "T5 queue drains once");

    /* T6: DSP/mixer compiled out -> absent from the sequence. */
    reset_build(0);
    tick(CSD_PW_IDLE_MS + 1u);
    expect_seq(DOWN_3, 3, "T6 shutdown order (dsp disabled)");

    /* T7: wake ORDER is the spec. */
    reset_build(1);
    tick(CSD_PW_IDLE_MS + 1u); drain(s, 16);
    csd_power_note_frame_activity();
    expect_seq(UP_4, 4, "T7 wake order (dsp enabled)");
    CK(csd_power_state() == CSD_PW_STATE_ON, "T7 back to ON after drain");
    reset_build(0);
    tick(CSD_PW_IDLE_MS + 1u); drain(s, 16);
    csd_power_note_frame_activity();
    expect_seq((const unsigned[]){CSD_PW_ACT_DAC_REINIT, CSD_PW_ACT_VOL_RESTORE,
                                  CSD_PW_ACT_AMP_ON}, 3, "T7b wake order (dsp disabled)");

    /* T8: the hard requirements as POSITION facts. */
    reset_build(1);
    tick(CSD_PW_IDLE_MS + 1u);
    int n = drain(s, 16), i;
    CK(n >= 1 && s[0] == CSD_PW_ACT_AMP_VOL_MIN, "T8a volume-min is FIRST down");
    CK(n >= 2 && s[1] == CSD_PW_ACT_AMP_OFF,     "T8b amp-off next");
    drain(s, 16);
    csd_power_note_frame_activity();
    n = drain(s, 16);
    int i_dac = -1, i_vol = -1, i_amp = -1;
    for (i = 0; i < n; i++) {
        if (s[i] == CSD_PW_ACT_DAC_REINIT)  i_dac = i;
        if (s[i] == CSD_PW_ACT_VOL_RESTORE) i_vol = i;
        if (s[i] == CSD_PW_ACT_AMP_ON)      i_amp = i;
    }
    CK(i_dac >= 0, "T8c DAC wake is a RE-INIT token, not an un-mute");
    CK(i_vol > i_dac, "T8d volume restored AFTER DAC re-init");
    CK(i_amp > i_vol, "T8e volume in place BEFORE amp enable (no pop)");

    /* T9: stream-stop is an independent trigger, and it does NOT bypass the
     * pop-suppression rule (alt-0 must still produce volume-min first). */
    reset_build(1);
    csd_power_note_stream_state(0 /*in*/, 0 /*out*/);
    tick(1u);
    expect_seq(DOWN_4, 4, "T9 host alt-0 stops in order, pop rule intact");

    /* T10: restart while actions are still queued must not re-trigger or
     * interleave a second sequence (the drain is single-flight). */
    reset_build(1);
    tick(CSD_PW_IDLE_MS + 1u);
    csd_power_note_frame_activity();          /* returns mid-shutdown */
    n = drain(s, 16);
    CK(n > 0, "T10 actions still produced after mid-shutdown return");
    for (i = 0; i < n; i++) if (s[i] == CSD_PW_ACT_AMP_VOL_MIN) break;
    CK(i == n || i == 0, "T10 no second volume-min injected mid-sequence");

    /* T11: all-zero frames are NOT activity (digital silence must time out). */
    reset_build(1);
    for (unsigned r = 0; r < CSD_PW_IDLE_MS + 5u; r++) {
        csd_power_note_frame_silence();
        tick(1u);                    /* time passes; silence never resets it */
    }
    CK(csd_power_state() != CSD_PW_STATE_ON, "T11 silence-only times out like no-stream");

    printf("%s: checks=%d fails=%d (IDLE_MS=%u)\n",
           fails ? "FAIL" : "PASS", checks, fails, (unsigned)CSD_PW_IDLE_MS);
    return fails ? 1 : 0;
}
