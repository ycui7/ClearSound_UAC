/* csd_power -- the ordered idle-power sequences (TODO item 7).
 *
 * Owner directive 2026-11-04. No audio stream for > 5 min -> bring the chain
 * down amplifier-first. On stream return -> DSP/mixer first, then a FULL codec
 * re-init, then volume restore, then the amplifier LAST (SUSPEND_N). Volume
 * goes to minimum BEFORE the amplifier drops (pop suppression).
 *
 * SHAPE, AND WHY IT IS THIS SMALL (owner, 2026-11-05: "i was thinking just a
 * well designed and tested sequence of actions to disable amp and re-enable
 * amp")
 * Two states and two sequences. No queue, no drain, no in-flight states. The
 * earlier cut modelled this as a 4-state machine with an 8-deep action ring
 * drained one step per 0.5 s poll, to keep a wake request from interleaving
 * with a shutdown. That hazard does not exist: only ONE task ever runs a
 * sequence, so a sequence cannot interleave with itself, and running it
 * straight through gives the same guarantee with no state to get wrong. The
 * ring was machinery for a problem the executor never had -- and one of its
 * transitions (activity arriving during the up drain) went untested and latched
 * the box ON forever, which is how it was found.
 *
 * THE WITNESS (the part that is easy to get wrong)
 * A running I2S clock is NOT a stream: the ES9219Q DPLL locks on clock alone
 * with nothing on the bus. Activity is therefore a NON-ZERO DAC-bound sample,
 * counted where the host stream is actually pulled (csd_pw_note_host_sample in
 * csd_power_state.c, fed from lib_xua's mixer read), with the buffer path's
 * non-zero underfill word excluded -- a stuck constant is not audio. Proven on
 * silicon, not asserted: with the box quiet the activity counter sat frozen at
 * 868140 while the frame counter climbed to 12.4 M in the same window
 * (2026-11-05), so silence cannot keep this box awake.
 *
 * The previous cut also carried two deliberately-empty witness hooks
 * (note_clock_alive, note_frame_silence) whose only purpose was to give a
 * wrong wiring somewhere to fail a test. Both are gone: nothing in this build
 * feeds a clock or an all-zero frame to the idle decision, so the rule now
 * holds by absence rather than by an empty function. If someone later wants an
 * alt-0 fast trigger (note_stream_state, which had no caller), the honest place
 * for it is here as another injected predicate, with a test -- not a hook that
 * nothing calls.
 *
 * HOW IT IS TESTED WITHOUT A BOARD
 * This file includes no xcore header and calls no firmware function. The two
 * things it needs from outside -- perform one step, and ask whether audio has
 * arrived -- are injected (csd_pw_ops_t). tests/test_csd_power.c records every
 * step a sequence emits and answers the activity poll however a case needs,
 * including "audio returned in the middle of teardown" and "audio was already
 * flowing when the wake ran", which the old suite never drove.
 *
 * THREADING
 * Exactly one task calls csd_power_poll. No locks, no atomics: single reader,
 * single writer by construction. Never call it from the audio thread -- it is
 * frame-budget territory, and the codec boot script it triggers blocks.
 */
#ifndef __csd_power_h__
#define __csd_power_h__

/* How often the assistant advances the clock. It rides an existing 0.5 s grid,
 * so this is 0.17 % of the timeout -- no new timer. */
#ifndef CSD_PW_TICK_MS
#define CSD_PW_TICK_MS (500u)
#endif

/* 5 minutes, per the owner. Compile-tunable so a bench can prove the sequences
 * in seconds: -DCSD_PW_IDLE_MS=20000. */
#ifndef CSD_PW_IDLE_MS
#define CSD_PW_IDLE_MS (300000u)
#endif

typedef enum csd_pw_state {
    CSD_PW_STATE_ON = 0,   /* chain up; the only state that counts idle time  */
    CSD_PW_STATE_OFF       /* amp + DAC + DSP down; wakes on real activity    */
} csd_pw_state_t;

/* The steps, in the order the owner specified. The assistant performs them;
 * this module decides WHICH and in what ORDER, which is the part worth a test.
 * Values are unchanged from the previous cut so old traces stay readable. */
typedef enum csd_pw_action {
    CSD_PW_ACT_NONE = 0,
    /* down: amplifier, DAC, DSP+mixer (volume floor first -- pop suppression) */
    CSD_PW_ACT_AMP_VOL_MIN,/* volume to minimum  (must precede AMP_OFF)       */
    CSD_PW_ACT_AMP_OFF,    /* amplifier disable                                */
    CSD_PW_ACT_DAC_OFF,    /* DAC down (mute + hold in reset)                  */
    CSD_PW_ACT_DSP_OFF,    /* DSP + mixer bypass        (if enabled)           */
    /* up: DSP+mixer, FULL re-init, volume, amplifier LAST */
    CSD_PW_ACT_DSP_ON,     /* DSP + mixer re-enable     (if enabled)           */
    CSD_PW_ACT_DAC_REINIT, /* full boot re-init, NOT an un-mute                */
    CSD_PW_ACT_VOL_RESTORE,/* prior volume re-applied   (after re-init)        */
    CSD_PW_ACT_AMP_ON      /* amplifier last: deassert SUSPEND_N               */
} csd_pw_action_t;

/* THE TWO THINGS THIS MODULE NEEDS FROM THE PLATFORM.
 * Declared here, defined by the firmware (csd_power_hooks.xc and
 * csd_power_state.c) and replaced by stubs in the host test. They are plain
 * externs rather than function pointers in a struct on purpose: xl1 bounds
 * every tile's stack at link time and will not price an indirect call without
 * [[fptrgroup]] on both the struct field and every callee (see the firmware
 * skill's stack-bound section), and a direct call has no such requirement. A
 * prototype costs nothing; an analyser workaround costs a build system.
 *
 * Both run in the assistant's context and MAY block -- that task has no
 * deadline (the codec boot script blocks for PLL settle and up to 20 chip-ID
 * retries, and nothing may block in the audio path, which is exactly why the
 * sequences live here and not in the mixer). */

/* Perform one step of a sequence. Called in the order the sequences below
 * request; must not reorder, merge or skip a step. */
void csd_pw_execute(unsigned act);

/* Non-zero if a real (non-zero, non-underfill) DAC-bound sample has arrived
 * since the previous call. Edge-consuming by contract: a second call in the
 * same poll slot reports only NEW audio, which is what lets a teardown ask
 * "did audio return while we were busy?". */
unsigned csd_pw_audio_arrived(void);

/* Build-time fact: was the DSP/mixer compiled in? Owner: "dsp, mixer start
 * first, if they are enabled". Set it before init. */
void csd_power_set_dsp_enabled(int enabled);

/* Boot. Idempotent: re-arms the ON state with a fresh timer. */
void csd_power_init(void);

/* THE WHOLE DECISION, once per poll grid slot. Advances the idle timer by
 * dt_ms and runs a sequence when one is due. A sequence always runs to
 * completion inside this call, which is what makes a half-torn-down codec
 * unrepresentable -- and if audio arrives while a teardown is executing, the
 * wake runs immediately after it, never in the middle. */
void csd_power_poll(unsigned dt_ms);

/* Diagnostics (the trace print and SCPI read these). */
csd_pw_state_t csd_power_state(void);
unsigned csd_power_idle_ms_dbg(void);

#endif /* __csd_power_h__ */
