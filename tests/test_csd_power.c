/* Host unit test for csd_power.c -- the ordered idle-power sequences
 * (TODO item 7, owner directive 2026-11-04; shape per owner, 2026-11-05:
 * "just a well designed and tested sequence of actions to disable amp and
 * re-enable amp").
 *
 *   gcc -O2 -DCSD_IDLE_POWER -I src/extensions tests/test_csd_power.c \
 *       src/extensions/csd_power.c -o /tmp/tpower && /tmp/tpower
 *
 * HOW IT WORKS. csd_power.c needs exactly two things from the platform --
 * perform a step, and ask whether audio has arrived -- and they are plain
 * externs (see csd_power.h for why they are not function pointers). This file
 * defines both: csd_pw_execute() RECORDS the steps in the order the module
 * asked for them, and csd_pw_audio_arrived() answers from a SCRIPT. That makes
 * every interesting case expressible, including the two the firmware could only
 * reach by luck before: "audio returned in the middle of teardown" and "audio
 * was already flowing when the wake ran".
 *
 * ORDER IS THE SPEC. Down: volume floor, amplifier, DAC, DSP+mixer. Up:
 * DSP+mixer, DAC, volume, amplifier LAST (SUSPEND_N). Hard requirements the
 * tests below name explicitly: DAC wake is a FULL re-init (never an un-mute),
 * volume is restored after that re-init but before the amplifier is re-enabled,
 * and the volume floor precedes the amplifier drop (pop suppression).
 *
 * THE WITNESS RULE. A free-running I2S clock proves NOTHING about stream
 * content -- the ES9219Q DPLL locks on clock alone with an empty bus, and a
 * build that trusted it never powers down. csd_power.c has no clock input at
 * all now, and T2 pins the consequence: the ONLY thing that can hold the box
 * up is csd_pw_audio_arrived(). (What counts as a real sample is decided one
 * level down, in csd_pw_note_host_sample, which excludes zero and the buffer
 * path's underfill word; that was confirmed on silicon by the activity counter
 * sitting frozen at 868140 while the frame counter climbed to 12.4 M.)
 *
 * WHY T11 IS THE IMPORTANT ONE. The previous cut latched a flag when audio
 * arrived during the wake drain and never cleared it, so the box powered down
 * and woke exactly once and then sat ON forever with the idle timer frozen --
 * measured on hardware as state=ON, idle=0, witness frozen. Its suite was green
 * because every wake test delivered activity while OFF, never while the up
 * sequence was running. Read that as a lesson about tests, not about state
 * machines.
 */
#include <stdio.h>
#include <stdint.h>
#include "csd_power.h"

static int fails = 0, checks = 0;
#define CK(cond, msg) do { checks++; if(!(cond)){ fails++; \
    printf("FAIL: %s  (line %d)\n", msg, __LINE__);} } while(0)

/* ---- platform stubs: the two externs csd_power.h declares ---------------- */

static unsigned rec[64]; static int nrec;   /* steps, in request order      */
static int asks;                            /* witness poll count           */
static unsigned live = 0;                   /* what the witness answers now */
/* The one thing a plain script cannot express: audio returning WHILE a
 * teardown is executing, i.e. between the deciding ask and the re-check ask
 * inside a single poll. Flipping it from the recorder after the Nth step is how
 * the test reaches that state without a thread. */
static unsigned flip_after_step = 0, steps_seen = 0;
/* And the reverse: go quiet again after N asks. Needed to test the idle timer
* INDEPENDENT of the wake trigger. Without it, every wake in the suite is
* caused by audio that keeps streaming, and that streaming resets the timer on
* its own -- which hid a real mutant: deleting the timer reset from the wake
* sequence survived, because the live audio behind the wake cleaned up after
* it. A single edge of audio, then silence, is the case that actually measures
* "five minutes counted from when the box became audible again". */
static unsigned quiet_after_asks = 0, quiet_base = 0;

/* Arm relative to NOW. Comparing against the global ask counter (which has
 * been counting since reset_build) made every later test answer 'quiet' on its
 * very first ask -- a silent wake, and a test that failed against a CORRECT
 * module. Any counter-based script must state whose count it is relative to. */
static void arm_quiet_after(unsigned n) { quiet_base = (unsigned)asks; quiet_after_asks = n; }

void csd_pw_execute(unsigned act)
{
    if (nrec < (int)(sizeof rec / sizeof rec[0])) rec[nrec++] = act;
    else { fails++; printf("FAIL: step log overflow\n"); }
    if (flip_after_step && ++steps_seen == flip_after_step) live = 1u;
}

unsigned csd_pw_audio_arrived(void)
{
    asks++;
    /* '>' not '>=': the counter was already advanced for THIS ask, so the
     * first ask after arming reads +1 and must still be allowed to answer
     * 'audio' -- with >= the arming silenced its own wake ask and the test
     * failed against a correct module. */
    if (quiet_after_asks && (unsigned)asks - quiet_base > quiet_after_asks) return 0u;
    return live;
}

static void script_quiet(void) { live = 0u; }
static void script_live(void)  { live = 1u; }

/* ---- helpers ------------------------------------------------------------- */

static void reset_build(int dsp_enabled)
{
    nrec = 0; asks = 0; live = 0u; flip_after_step = 0; steps_seen = 0;
    quiet_after_asks = 0; quiet_base = 0;
    csd_power_set_dsp_enabled(dsp_enabled);
    /* The timeout is RUNTIME state now (case 14), and csd_power_init() does
     * not touch it -- without this every test after the first that set a
     * timeout would inherit it and measure the previous case's window. */
    csd_power_timeout_restore((int)(CSD_PW_IDLE_MS / 1000u));
    csd_power_init();
}

/* The assistant's grid: poll() is called once per slot, never more often. */
static void poll(unsigned n_slots)
{
    for (unsigned i = 0; i < n_slots; i++) csd_power_poll(CSD_PW_TICK_MS);
}
static void poll_quiet_to_timeout(void)
{
    poll((CSD_PW_IDLE_MS / CSD_PW_TICK_MS) + 1u);
}

static void expect_seq(const unsigned *want, int n, const char *what)
{
    checks++;
    if (nrec != n) {
        fails++;
        printf("FAIL: %s: got %d steps, want %d:", what, nrec, n);
        for (int i = 0; i < nrec; i++) printf(" %u", rec[i]);
        printf("\n");
        nrec = 0;
        return;
    }
    for (int i = 0; i < n; i++) {
        checks++;
        if (rec[i] != want[i]) {
            fails++;
            printf("FAIL: %s: step[%d]=%u want %u\n", what, i, rec[i], want[i]);
            break;
        }
    }
    nrec = 0;
}

int main(void)
{
    const unsigned DOWN_4[] = {CSD_PW_ACT_AMP_VOL_MIN, CSD_PW_ACT_AMP_OFF,
                               CSD_PW_ACT_DAC_OFF,     CSD_PW_ACT_DSP_OFF};
    const unsigned DOWN_3[] = {CSD_PW_ACT_AMP_VOL_MIN, CSD_PW_ACT_AMP_OFF,
                               CSD_PW_ACT_DAC_OFF};
    const unsigned UP_4[]   = {CSD_PW_ACT_DSP_ON,      CSD_PW_ACT_DAC_REINIT,
                               CSD_PW_ACT_VOL_RESTORE, CSD_PW_ACT_AMP_ON};
    const unsigned UP_3[]   = {CSD_PW_ACT_DAC_REINIT,  CSD_PW_ACT_VOL_RESTORE,
                               CSD_PW_ACT_AMP_ON};
    /* T9's single-poll record: the whole down sequence, then the whole up. */
    const unsigned DOWN_THEN_UP[] = {CSD_PW_ACT_AMP_VOL_MIN, CSD_PW_ACT_AMP_OFF,
                                     CSD_PW_ACT_DAC_OFF,     CSD_PW_ACT_DSP_OFF,
                                     CSD_PW_ACT_DSP_ON,      CSD_PW_ACT_DAC_REINIT,
                                     CSD_PW_ACT_VOL_RESTORE, CSD_PW_ACT_AMP_ON};

    /* T1: boot is ON and nothing has been executed. */
    reset_build(1);
    CK(csd_power_state() == CSD_PW_STATE_ON, "T1 boot state ON");
    CK(nrec == 0, "T1 init executes no steps");

    /* T2: THE WITNESS RULE. With the witness answering 'silent bus' -- which is
     * what a live LRCLK with no samples looks like -- the box MUST time out.
     * This is the whole reason the feature was ever broken for a week; a build
     * that trusted the clock passes every other test and burns idle power
     * forever. There is no clock input left to trust, and this test is what
     * keeps it that way: csd_pw_audio_arrived() is the module's only input. */
    reset_build(1);
    script_quiet();
    poll_quiet_to_timeout();
    expect_seq(DOWN_4, 4, "T2 silent bus times out (clock is NOT a witness)");
    CK(csd_power_state() == CSD_PW_STATE_OFF, "T2 ends OFF");

    /* T3: the bound is exact and strict, measured in real poll slots. */
    reset_build(1);
    poll((CSD_PW_IDLE_MS / CSD_PW_TICK_MS) - 1u);
    CK(csd_power_state() == CSD_PW_STATE_ON, "T3 one slot short of 5 min: still ON");
    CK(nrec == 0, "T3 no steps early");
    poll(1);
    expect_seq(DOWN_4, 4, "T3 fires exactly at the bound");

    /* T4: sparse REAL audio holds the box up indefinitely. Each burst must
     * restart the clock, so this must not drift into a shutdown. */
    reset_build(1);
    for (unsigned r = 0; r < 10u; r++) {
        script_live();  poll(2);            /* a few frames of genuine audio */
        script_quiet(); poll((CSD_PW_IDLE_MS / CSD_PW_TICK_MS) - 2u);
    }
    CK(csd_power_state() == CSD_PW_STATE_ON, "T4 sparse real audio never powers down");
    CK(nrec == 0, "T4 executed nothing while audio kept returning");

    /* T5/T6: shutdown ORDER, both build shapes (owner: DSP/mixer only IF built). */
    reset_build(1); poll_quiet_to_timeout();
    expect_seq(DOWN_4, 4, "T5 shutdown order (dsp built in)");
    CK(csd_power_state() == CSD_PW_STATE_OFF, "T5 ends OFF");
    poll(10);
    CK(nrec == 0, "T5 OFF is stable -- no repeated teardown while quiet");

    reset_build(0); poll_quiet_to_timeout();
    expect_seq(DOWN_3, 3, "T6 shutdown order (dsp compiled out)");

    /* T7: wake ORDER, both shapes, and the state lands back ON. */
    reset_build(1); poll_quiet_to_timeout(); nrec = 0;
    script_live(); poll(1);
    expect_seq(UP_4, 4, "T7 wake order (dsp built in)");
    CK(csd_power_state() == CSD_PW_STATE_ON, "T7 back ON after the wake");

    reset_build(0); poll_quiet_to_timeout(); nrec = 0;
    script_live(); poll(1);
    expect_seq(UP_3, 3, "T7b wake order (dsp compiled out)");

    /* T8: the safety-critical orderings stated as POSITION facts, so a
     * reordering that still produces the right SET cannot pass. */
    reset_build(1); poll_quiet_to_timeout();
    CK(nrec >= 2 && rec[0] == CSD_PW_ACT_AMP_VOL_MIN,
       "T8a volume floor is FIRST down (pop suppression)");
    CK(nrec >= 3 && rec[1] == CSD_PW_ACT_AMP_OFF,
       "T8b amplifier drops next, while the DAC is still powered");
    nrec = 0;
    script_live(); poll(1);
    int i_dac = -1, i_vol = -1, i_amp = -1;
    for (int i = 0; i < nrec; i++) {
        if (rec[i] == CSD_PW_ACT_DAC_REINIT)  i_dac = i;
        if (rec[i] == CSD_PW_ACT_VOL_RESTORE) i_vol = i;
        if (rec[i] == CSD_PW_ACT_AMP_ON)      i_amp = i;
    }
    CK(i_dac >= 0, "T8c DAC wake is a FULL RE-INIT token, not an un-mute");
    CK(i_vol > i_dac, "T8d volume restored AFTER the re-init (into a live chip)");
    CK(i_amp > i_vol, "T8e volume in place BEFORE the amplifier (no pop)");
    CK(i_amp == nrec - 1, "T8f amplifier is the LAST step up");

    /* T9: audio returns WHILE the teardown is executing. Two asks happen in the
     * poll that fires: one to decide, one after the chain is down. Script them
     * explicitly -- silent for the decision, live for the re-check. This is the
     * case the deleted action ring existed to protect, and the record proves a
     * straight-line implementation protects it just as well: the up sequence
     * follows the COMPLETE down sequence, interleaving is not reachable. */
    reset_build(1);
    /* Silent while the decision is made; the recorder flips the witness after
     * the 4th down step, which is the instant between the deciding ask and the
     * re-check ask. No thread, no race -- the state is reached on purpose. */
    script_quiet();
    flip_after_step = 4;
    /* Land exactly ON the firing slot. Using a poll count that overshoots by
     * one would let the NEXT poll perform the wake from the OFF branch and
     * record the same eight steps for the wrong reason -- measured: deleting
     * the post-teardown re-check entirely still passed T9 until the arithmetic
     * was pinned, and only the ask count exposed it. */
    poll((CSD_PW_IDLE_MS / CSD_PW_TICK_MS) - 1u);
    CK(nrec == 0, "T9 nothing before the firing slot");
    poll(1);
    expect_seq(DOWN_THEN_UP, 8, "T9 audio during teardown -> full down then full up IN ONE POLL");
    CK(csd_power_state() == CSD_PW_STATE_ON, "T9 ends ON, never left half-down");

    /* T10: THE HARDWARE BUG. The wake is caused by a host that is ALREADY
     * streaming, so the witness keeps answering 'live' for the whole wake and
     * for a while after. The previous cut latched that and froze the idle timer
     * forever; this test drives exactly that shape and then requires a SECOND
     * shutdown. */
    reset_build(1);
    poll_quiet_to_timeout();
    CK(nrec == 4, "T10 first shutdown happened");
    nrec = 0;
    script_live(); poll(4);                  /* streaming throughout the wake */
    expect_seq(UP_4, 4, "T10 wake completed while audio kept arriving");
    CK(csd_power_state() == CSD_PW_STATE_ON, "T10 ON after the wake");
    script_quiet();
    poll((CSD_PW_IDLE_MS / CSD_PW_TICK_MS) - 1u);
    CK(csd_power_state() == CSD_PW_STATE_ON, "T10 idle clock RESTARTED at the wake");
    poll(1);
    expect_seq(DOWN_4, 4, "T10 RE-ARMS: a second shutdown happens after a live wake");
    CK(csd_power_state() == CSD_PW_STATE_OFF, "T10 reaches OFF a second time");

    /* T10b: the idle timer is measured from the wake, not from the streaming
     * that happened to cause it. ONE edge of audio triggers the wake and then
     * the bus goes silent for the whole wake and beyond, so nothing else can
     * restart the clock. This is the case that catches a wake sequence which
     * forgets to zero the timer: with streaming audio present the omission is
     * invisible, and a mutation test proved it survived without this case. */
    reset_build(1);
    poll_quiet_to_timeout();
    CK(csd_power_state() == CSD_PW_STATE_OFF, "T10b down first");
    nrec = 0;
    script_live();
    arm_quiet_after(1);                   /* one ask says 'audio', the rest quiet */
    /* Exactly ONE slot: the OFF branch asks once and wakes. Extra slots here
     * would be quiet slots that spend the idle budget and make the assertions
     * below measure the wrong window. */
    poll(1);
    expect_seq(UP_4, 4, "T10b woke on a single edge of audio");
    CK(csd_power_state() == CSD_PW_STATE_ON, "T10b ON after the wake");
    poll((CSD_PW_IDLE_MS / CSD_PW_TICK_MS) - 1u);
    CK(csd_power_state() == CSD_PW_STATE_ON,
       "T10b idle clock restarted AT the wake, not carried over from the outage");
    poll(1);
    CK(csd_power_state() == CSD_PW_STATE_OFF, "T10b times out on its own schedule");

    /* T11: THREE cycles with real audio in every ON window. Repeating the
     * transition is what catches a latch that survives one pass. */
    reset_build(1);
    for (int cyc = 0; cyc < 3; cyc++) {
        char tag[64];
        snprintf(tag, sizeof tag, "T11 cycle %d goes down", cyc);
        script_quiet(); poll_quiet_to_timeout(); expect_seq(DOWN_4, 4, tag);
        snprintf(tag, sizeof tag, "T11 cycle %d comes up", cyc);
        script_live(); poll(3); expect_seq(UP_4, 4, tag);
        snprintf(tag, sizeof tag, "T11 cycle %d holds awake on real audio", cyc);
        for (unsigned r = 0; r < 5u; r++) { script_live(); poll(2);
                                           script_quiet(); poll((CSD_PW_IDLE_MS / CSD_PW_TICK_MS) - 3u); }
        CK(csd_power_state() == CSD_PW_STATE_ON, tag);
    }

    /* T12: OFF is quiet. No audio, no steps, no oscillation, no repeated
     * actuator traffic that would click a relay or re-mute a muted codec. */
    reset_build(1);
    poll_quiet_to_timeout(); nrec = 0;
    script_quiet(); poll(2000);
    CK(csd_power_state() == CSD_PW_STATE_OFF, "T12 stays OFF while silent");
    CK(nrec == 0, "T12 no actuator traffic while OFF");

    /* T13: the module asks the witness at most twice per slot. More than that
     * means a hidden second input or a loop inside a decision, both of which
     * cost time in a task that also runs the blocking codec boot script. */
    reset_build(1);
    script_quiet(); poll(1);
    CK(asks == 1, "T13 one witness ask per ordinary slot");
    /* Walk right up to the slot that fires, then count that slot alone. */
    /* One slot short of the firing slot. The first poll above already spent
     * one, and the check is idle+dt >= IDLE, so the arithmetic has to land on
     * the slot BEFORE the one that fires or asks counts the wrong slot. */
    poll((CSD_PW_IDLE_MS / CSD_PW_TICK_MS) - 2u);
    asks = 0;
    flip_after_step = 4;          /* so the firing slot also re-wakes */
    poll(1);
    CK(asks == 2, "T13 exactly two asks in the slot that tears down and re-wakes");

    /* ---- owner's case-14 timeout semantics (directive 2026-11-05) ----------
     * Three regimes, each able to fail on its own: 0 = sleep now and stay
     * asleep, negative = never sleep, positive = that many seconds. A module
     * that only implemented the positive case would pass everything above. */

    /* T15: the default is the owner's 300 s (scaled by the build's IDLE_MS). */
    reset_build(1);
    CK(csd_power_get_timeout_sec() == (int)(CSD_PW_IDLE_MS / 1000u),
       "T15 default timeout is the compile-time default");

    /* T16: 0 tears the chain down on the NEXT tick -- not after a full window. */
    reset_build(1);
    csd_power_set_timeout_sec(0);
    script_quiet(); poll(1);
    expect_seq(DOWN_4, 4, "T16 set 0 tears down on the next tick");
    CK(csd_power_state() == CSD_PW_STATE_OFF, "T16 OFF immediately after set 0");

    /* T17: and it stays asleep indefinitely while silent -- the timeout must
     * not quietly re-arm itself -- then wakes on real audio. */
    nrec = 0; script_quiet(); poll(500);
    CK(nrec == 0 && csd_power_state() == CSD_PW_STATE_OFF,
       "T17 set 0 stays asleep indefinitely while silent");
    script_live(); poll(1);
    expect_seq(UP_4, 4, "T17 set 0 still wakes on real audio");

    /* T18: negative never sleeps, however long the box is silent. */
    reset_build(1);
    csd_power_set_timeout_sec(-1);
    script_quiet(); poll((CSD_PW_IDLE_MS / CSD_PW_TICK_MS) * 2u);
    CK(nrec == 0, "T18 negative never powers down");
    CK(csd_power_state() == CSD_PW_STATE_ON, "T18 stays ON under negative");
    CK(csd_power_get_timeout_sec() == -1, "T18 readback reports the negative setting");
    /* The observable difference from merely casting to unsigned -- which also
     * never fires, and is why deleting the guard is otherwise an EQUIVALENT
     * mutant: csd_pw_audio_arrived() consumes its edge, so a box that must
     * never sleep must not spend the witness. T18b asserts that directly. */
    reset_build(1);
    csd_power_set_timeout_sec(-1);
    script_live();  poll((CSD_PW_IDLE_MS / CSD_PW_TICK_MS) * 2u);
    CK(asks == 0, "T18b negative never polls the edge-consuming witness");
    CK(nrec == 0 && csd_power_state() == CSD_PW_STATE_ON,
       "T18c negative holds the box up even with audio flowing");
    /* Leaving the negative regime must return the module to normal behaviour,
     * not leave it stuck: stop the audio, wait the window, it tears down; audio
     * returns, it wakes. A guard that returned from the wrong place would fail
     * here rather than in T18. */
    csd_power_set_timeout_sec(1);
    script_quiet(); poll((1000u / CSD_PW_TICK_MS) + 2u);
    expect_seq(DOWN_4, 4, "T18d leaves the negative regime and times out normally");
    script_live(); poll(1);
    expect_seq(UP_4, 4, "T18e wakes after leaving the negative regime");

    /* T19: a positive value means exactly that many seconds, counted from the
     * set (not from the last sample), and fires no earlier. */
    reset_build(1);
    csd_power_set_timeout_sec(5);
    script_quiet(); poll((5000u / CSD_PW_TICK_MS) - 1u);
    CK(nrec == 0, "T19 does not fire before the set timeout");
    poll(1);
    expect_seq(DOWN_4, 4, "T19 fires at the set timeout");

    /* T21: a live set() DOES restart the countdown -- without a case that has
     * already spent some of the window, "set" and "restore" are the same
     * function as far as the suite is concerned and either could be deleted. */
    reset_build(1);
    script_quiet(); poll(4u);                      /* 2 s already idle */
    csd_power_set_timeout_sec(5);                  /* must measure from NOW */
    poll((5000u / CSD_PW_TICK_MS) - 1u);           /* 4.5 s after the set */
    CK(nrec == 0, "T21 live set restarts the countdown (no early fire)");
    poll(1);
    expect_seq(DOWN_4, 4, "T21 fires a full window after the set");

    /* T20: the boot restore path must adopt the value WITHOUT restarting the
     * countdown, or a restore landing late would silently extend the window.
     * Distinguishes restore() from set(), which does restart. */
    reset_build(1);
    csd_power_set_timeout_sec(5);
    script_quiet(); poll(4u);                      /* 2 s of the 5 s spent */
    csd_power_timeout_restore(5);
    poll((5000u / CSD_PW_TICK_MS) - 4u);           /* remaining 3 s */
    expect_seq(DOWN_4, 4, "T20 restore does not restart the countdown");

    printf("%s: checks=%d fails=%d (IDLE_MS=%u TICK_MS=%u)\n",
           fails ? "FAIL" : "PASS", checks, fails,
           (unsigned)CSD_PW_IDLE_MS, (unsigned)CSD_PW_TICK_MS);
    return fails ? 1 : 0;
}
