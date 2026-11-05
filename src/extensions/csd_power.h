/* csd_power -- ordered idle-power state machine (TODO item 7).
 *
 * Owner directive 2026-11-04. No audio stream for > 5 min -> power down in
 * this order: amplifier, DAC, DSP+mixer. On stream return, in this order:
 * DSP+mixer (if enabled), DAC, amplifier last (deassert SUSPEND_N). DAC wake
 * means a FULL re-initialisation of the boot sequence, not an un-mute, with
 * volume restored to the pre-shutdown value, and volume driven to minimum
 * BEFORE the amplifier is dropped (pop suppression).
 *
 * WHY A SEPARATE PURE-C MODULE
 * The whole feature is a state machine plus an action queue. Keeping it free
 * of xcore headers means the ordering -- the part that can hurt a listener's
 * ears and a DAC -- is testable on the host in milliseconds, the same
 * argument that made csd_vol_mailbox.c a C file. The firmware side only
 * supplies witnesses and executes actions; it holds no policy.
 *
 * THE WITNESS (this is the part that is easy to get wrong)
 * A running I2S clock is NOT evidence of a stream: the ES9219Q DPLL locks on
 * clock alone with no samples on the bus. So activity is a NON-ZERO received
 * sample, counted where samples are actually pulled from the host
 * (csd_mb_get_host_nonzero, fed by lib_xua mixer1 GetSamplesFromHost). A
 * paused stream, a host that stopped sending ISO packets, and a muted codec
 * all freeze that counter, which is the correct answer for each.
 * csd_power_note_clock_alive() exists ONLY so a wiring mistake that trusts
 * the clock has somewhere to land and fails a test (T2) instead of silently
 * burning idle power forever.
 *
 * THREADING
 * Exactly one task (the tile[1] power poll task) calls tick, the note functions, and next_action --
 * task. No locks, no atomics: this module has a single reader and a single
 * writer by construction. Do not call it from the audio thread -- it is
 * frame-budget territory, and the witnesses are already counted for us.
 */
#ifndef __csd_power_h__
#define __csd_power_h__

/* How often the firmware executor advances the clock. The executor rides an
 * existing 0.5 s grid, so this is 0.17% of the timeout -- no new timer. */
#ifndef CSD_PW_TICK_MS
#define CSD_PW_TICK_MS (500u)
unsigned csd_power_idle_ms_dbg(void);   /* DIAGNOSTIC (temporary) */
unsigned csd_power_dbg_flags(void);     /* bit0 activity_seen, bit1 stream_stopped, bits 8+ state */

#endif

/* 5 minutes, per the owner. Compile-tunable so a bench can prove the
 * sequence in seconds: -DCSD_PW_IDLE_MS=5000. */
#ifndef CSD_PW_IDLE_MS
#define CSD_PW_IDLE_MS (300000u)
#endif

typedef enum csd_pw_state {
    CSD_PW_STATE_ON = 0,   /* everything powered, audio flowing or not yet idle */
    CSD_PW_STATE_WAIT_DOWN,/* idle expired; the DOWN action queue is draining */
    CSD_PW_STATE_WAIT_UP,  /* the UP action queue is draining */
    CSD_PW_STATE_OFF       /* amp+DAC+DSP down; wakes on the next activity */
} csd_pw_state_t;

/* Actions are REQUESTS to the firmware executor, in the order the owner
 * specified. The queue is single-flight: nothing is produced while a
 * sequence is mid-drain, so a stream that returns during shutdown completes
 * the shutdown and then wakes -- a half-torn-down codec is never left
 * standing, and the pop-suppression rule is never skipped. */
typedef enum csd_pw_action {
    CSD_PW_ACT_NONE = 0,
    /* down */
    CSD_PW_ACT_AMP_VOL_MIN,/* volume to minimum  (must precede AMP_OFF) */
    CSD_PW_ACT_AMP_OFF,    /* amplifier disable                          */
    CSD_PW_ACT_DAC_OFF,    /* DAC down (mute + soft reset)               */
    CSD_PW_ACT_DSP_OFF,    /* DSP + mixer bypass        (if enabled)     */
    /* up */
    CSD_PW_ACT_DSP_ON,     /* DSP + mixer re-enable     (if enabled)     */
    CSD_PW_ACT_DAC_REINIT, /* FULL boot re-init, NOT an un-mute          */
    CSD_PW_ACT_VOL_RESTORE,/* prior volume re-applied   (after re-init)  */
    CSD_PW_ACT_AMP_ON      /* amplifier last: deassert SUSPEND_N         */
} csd_pw_action_t;

/* Build-time fact: was the DSP/mixer compiled in? Owner: "dsp, mixer start
 * first, if they are enabled". Called once at boot before init. */
void csd_power_set_dsp_enabled(int enabled);

/* Boot. Idempotent: calling it again re-arms the ON state and clears any
 * queued actions. */
void csd_power_init(void);

/* Advance time by dt_ms. Call from a slow poll, never from the audio path. */
void csd_power_tick(unsigned dt_ms);

/* WITNESS: a non-zero sample was received from the host since the last call.
 * Called from the poll task when the host-nonzero counter has advanced. */
void csd_power_note_frame_activity(void);

/* WITNESS (inert by design): a frame arrived whose content was ALL ZERO.
 * Digital silence is not audio: a host streaming zeros (or a muted codec)
 * must time out exactly like an absent stream, or an unattended muted DAC
 * idles forever. Present so the poll task can report 'frame seen, empty'
 * distinctly from 'no frame seen' for diagnostics; it must never feed the
 * idle decision. Host test T11 fails the moment anyone makes it count. */
void csd_power_note_frame_silence(void);

/* WITNESS (inert by design): the I2S clock is toggling. This MUST NOT affect
 * the idle decision -- see the witness rule above. Present so the wiring has
 * one obvious place to put a clock-derived signal and be wrong loudly. */
void csd_power_note_clock_alive(void);

/* WITNESS: USB SET_INTERFACE alt-setting change from the host (1 = streaming
 * alt, 0 = alt 0 == stream stop). alt 0 arms the shutdown immediately: the
 * host said it stopped, and waiting another 5 minutes for a codec that is
 * already drawing idle power is not what the owner asked for. */
void csd_power_note_stream_state(int input_active, int output_active);

/* Query for the executor and diagnostics (SCPI reads this). */
csd_pw_state_t csd_power_state(void);

/* Pop the next action for the executor. Returns CSD_PW_ACT_NONE when the
 * queue is empty. The executor performs it and calls again; a single-flight
 * invariant means only one sequence is ever in flight. */
unsigned csd_power_next_action(void);

#endif /* __csd_power_h__ */
