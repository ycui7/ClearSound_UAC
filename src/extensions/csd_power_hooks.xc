/* Idle-power wiring (TODO item 7). Owner directive 2026-11-04: after 5
 * minutes with no host audio, bring the chain down amplifier-first, and bring
 * it back DSP/mixer first, then a full codec re-init, then volume restore,
 * then the amplifier last.
 *
 * WHAT THIS FILE IS, AFTER THE 2026-11-05 SIMPLIFICATION
 * Policy lives in csd_power.c: two states, two sequences, host-tested without a
 * board. This file is the wiring -- the witness, the actuator executor, and the
 * 0.5 s grid that makes one call, csd_power_poll(). It decides nothing. The
 * action ring and the per-slot drain it used to hold are gone; see csd_power.h
 * for why the single-flight guarantee they were meant to provide came for free
 * all along.
 *
 * THE TILE RULE THIS WHOLE FILE IS BUILT AROUND
 * The witness counters are plain C statics, and a C static is NOT shared
 * between tiles. Measured on hardware 2026-11-05 with the first cut of this
 * feature: a writer on one tile and a reader on the other got 21,925,082 and 0
 * from the same variable, with no error on either side. So the policy lives
 * with its witnesses, on the audio/mixer core (tile[1]) -- the mixer's raw
 * host read (lib_xua) and the DSP bypass flag the audio thread adopts are both
 * tile[1] state -- and only actuator COMMANDS cross to the codec server on
 * tile[0]. A channel is the only mechanism that crosses tiles; that one
 * measurement is why this file looks the way it does.
 *
 * WHY THE POLL RUNS AS ITS OWN TASK ON tile[1]
 * It has to be a task on the witness tile, and it cannot be folded into the
 * audio thread or the mixer: the codec boot script the wake sequence runs
 * BLOCKS (PLL settle, up to 20 chip-ID retries at 5 ms, and on a dead board a
 * 2 s LED-bypass render), and neither the audio thread nor mixer1 may ever
 * block -- the audiohub handshake is lock-step. A dedicated task with no
 * deadline is the only place a blocking re-init is free.
 *
 * WHY TRAFFIC IS ONE-WAY (assistant -> server) AND NEVER AWAITED
 * A `chan <: x` blocks until the far end selects to receive. The codec server
 * services this core's I2C traffic, and the boot script run here goes through
 * that service. If the server ever sent a message here, each side could wait
 * on the other while holding what the other needs -- exactly the deadlock the
 * first attempt produced. One-way traffic makes that unrepresentable: the
 * assistant sends, the server executes, and a dedicated chanend preserves the
 * order of a sequence (VOL_MIN -> AMP_OFF -> DAC_OFF) without any handshake.
 */
#include "platform.h"
#include "xs1.h"
#include "print.h"
#include <debug_print.h>
#include "csd_power_state.h"
#include "csd_power.h"
#include "csd_gen.h"
#include "clearsounddac_board.h"

/* The power chanend, held for the life of the task.
 *
 * Stored once at entry from the task argument, which is the same shape as the
 * proven uc_audiohw1 bindings in user_main.h: the resource is STORED, never
 * passed on. It is deliberately not a parameter of the executor -- passing a
 * global unsafe chanend into a safe chanend parameter compiles to a checked
 * demotion that XMOS implements as a runtime ecall, measured on hardware
 * 2026-11-05 as ET_ECALL at main.xc:546 in this very task, with USB never
 * enumerating. One task owns it, so there is no second writer to race and
 * nothing for the branch checker to disagree about. */
static unsafe chanend pw_chan;

/* One actuator command out to the codec server.
 *
 * Wrapped in `unsafe` because pw_chan is an unsafe resource and XC requires an
 * unsafe region to touch one -- same shape as GPIO_WR() in
 * clearsounddac_board.xc. One command word only: the actuator commands carry no
 * payload. Ordering across calls is guaranteed by the dedicated chanend, so the
 * down sequence (VOL_MIN, AMP_OFF, DAC_OFF) and the wake sequence (VOL_RESTORE,
 * AMP_ON) each arrive in order with no handshake and no acknowledgement.
 *
 * Print BEFORE the send, at the call sites below: with the print after, a send
 * that never completed produced no output at all and the shutdown looked like
 * it had never started (measured 2026-11-05: trace froze at idle=19500, exactly
 * the tick that issues the volume-min command). */
static inline void pw_send(unsigned cmd)
{
    unsafe { pw_chan <: cmd; }
}

/* ---- csd_pw_execute: perform one step of a sequence ----------------------
 * The platform half of the contract in csd_power.h. csd_power.c decides the
 * ORDER and its host test asserts that order; this switch says only how each
 * step is carried out. A plain extern, not a function pointer in a struct:
 * xl1 will not price an indirect call without [[fptrgroup]], and the linker
 * then also catches a firmware build that forgot to provide an executor. */
void csd_pw_execute(unsigned act)
{
    switch (act) {
        case CSD_PW_ACT_AMP_VOL_MIN:
            debug_printf("CSD_PW: amp-down pre-step, volume to minimum\n");
            pw_send(CSD_PW_CMD_VOL_MIN);
            break;
        case CSD_PW_ACT_AMP_OFF:
            debug_printf("CSD_PW: amplifier disabled\n");
            pw_send(CSD_PW_CMD_AMP_OFF);
            break;
        case CSD_PW_ACT_DAC_OFF:
            debug_printf("CSD_PW: DAC muted + held in reset\n");
            pw_send(CSD_PW_CMD_DAC_OFF);
            break;
        case CSD_PW_ACT_DSP_OFF:
            /* What this does: the biquad bypass (preset 0 = identity). The
             * mixer MULTIPLIER cannot be stopped at runtime -- the audiohub
             * handshake is lock-step, so stopping it is a task lifecycle
             * change, not a register write. Claiming otherwise would be a
             * fake; the VPU work, which is the real power cost, is gone.
             *
             * Posted as a LOCAL flag, not a command: the audio thread that
             * adopts it is on THIS tile, so a same-tile static is correct here
             * and a channel would be waste. It is not a call into csd_dsp.xc
             * either: an &-to-XC-static inside a called XC function is
             * attributed by the branch checker to the CALLER's par branch, and
             * the linker then refuses the image. */
            csd_pw_request_dsp_bypass(1u);
            debug_printf("CSD_PW: DSP bypassed (mixer keeps running)\n");
            break;
        case CSD_PW_ACT_DSP_ON:
            csd_pw_request_dsp_bypass(0u);   /* audio thread adopts */
            debug_printf("CSD_PW: DSP restored\n");
            break;
        case CSD_PW_ACT_DAC_REINIT:
            /* FULL RE-INIT BY REUSE: the SAME entry point USB boot calls
             * (owner: "when you restart DAC, don't forget to re-initialize
             * it"). A hand-written lighter init would drift from the boot
             * sequence the day either changed; reuse is the honest form of
             * "full".
             *
             * Blocks right here -- this task has no deadline, and the codec
             * server must stay free to service the I2C these writes travel
             * over.
             *
             * No AudioHwConfig() alongside it, deliberately: that reaches
             * csd_dsp_set_rate, which writes the audio branch's csd_dsp
             * statics (the branch checker refuses this par branch), and
             * lib_xua already runs the full AudioHwConfig_Mute / AudioHwConfig
             * / _UnMute sequence itself whenever the host (re)opens a stream
             * (xua_audiohub.xc:1013-1038) -- a precondition of ever getting
             * here, since the wake completes only on real samples arriving.
             *
             * Ordering guarantee: the channel preserves message order, so
             * VOL_RESTORE below cannot overtake this boot script -- the server
             * applies the level only after the chip is actually re-initialised. */
            debug_printf("CSD_PW: full codec re-init\n");
            {
                csd_config_t cfg;
                cfg.default_mclk = csd_pw_last_mclk();
                csd_AudioHwInit(cfg);
            }
            break;
        case CSD_PW_ACT_VOL_RESTORE:
            debug_printf("CSD_PW: volume restored after re-init\n");
            pw_send(CSD_PW_CMD_VOL_RESTORE);
            break;
        case CSD_PW_ACT_AMP_ON:
            /* LAST: deassert the amplifier (owner: SUSPEND_N deasserted last,
             * and never before the level is back -- restoring volume with the
             * amp live is a pop). */
            debug_printf("CSD_PW: amplifier enabled\n");
            pw_send(CSD_PW_CMD_AMP_ON);
            break;
        default:
            break;
    }
}

/* csd_pw_audio_arrived() -- the witness -- lives with the counters it reads,
 * in csd_power_state.c, not here: the cursor is that module's own state, and
 * keeping it there means the ask-after-a-teardown question ("did audio return
 * while we were busy?") is answered by the same code that counts samples.
 * The counters are tile[1] C statics written by the mixer as host words are
 * pulled, so both the counter and the cursor are same-tile and true. */

void csd_PowerAssistantTask(unsafe chanend c_pw)
{
    timer t;
    unsigned now;

    pw_chan = c_pw;

    csd_power_set_dsp_enabled(PCM_DSP_EN);   /* owner's wake rule: DSP/mixer
                                              * start first IF they are built */
    csd_power_init();

    t :> now;
    while (1) {
        select {
            case t when timerafter(now) :> void:
                now += 500u * XS1_TIMER_KHZ;        /* 0.5 s policy grid */
#ifdef CSD_PW_TRACE
                {   /* ~every 10 s. Prints the quantities the decision is made
                     * from: the raw witness and the frame count (which together
                     * separate 'streaming' from 'clock alive, bus silent'), the
                     * state, and the idle clock against its bound. The re-arm
                     * bug was visible in precisely these numbers -- state ON
                     * with idle pinned at 0 while the witness stood still. */
                    static unsigned tr = 0;
                    if ((++tr % 20u) == 1u) {
                        unsigned a_  = csd_pw_host_activity();
                        unsigned f_  = csd_pw_host_frames();
                        int      st_ = (int)csd_power_state();
                        unsigned id_ = csd_power_idle_ms_dbg();
                        unsigned bd_ = CSD_PW_IDLE_MS;
                        unsigned mc_ = csd_pw_last_mclk();
                        /* Hoisted rather than inline: this line is only
                         * reachable on a clean build, and a malformed -D on the
                         * command line shows up here as a parse error at the
                         * macro use. Local variables make that readable, and
                         * keep each debug_printf well inside its argument
                         * limit. Same prefix so the rig can rejoin the halves. */
                        debug_printf("PWTRACE act=%u frames=%u state=%d\n", a_, f_, st_);
                        debug_printf("PWTRACE  idle=%u/%u mclk=%u tick=%u\n", id_, bd_, mc_, tr);
                    }
                }
#endif
                /* The whole decision, once per slot. A sequence that becomes
                 * due runs to completion inside this call, which is what makes
                 * a half-torn-down codec an unreachable state. */
                csd_power_poll(CSD_PW_TICK_MS);
            break;
        }
    }
}

/* NO alt-0 / UserAudioStreamState hook, deliberately.
 * An earlier cut added one as a FAST shutdown trigger. It is not in the
 * requirement ('no audio stream coming for more than 5 min'), it had no caller
 * wired in the shipping tree, and it needed a cross-tile path that has only two
 * possible shapes, both wrong: a C static written on tile[0] and read on
 * tile[1] (measured: silently reads 0), or a codec-server -> assistant send
 * (deadlock, because the codec boot script the assistant runs is served I2C BY
 * that server). The silence witness above already satisfies the requirement on
 * its own tile with no channel in the trigger path at all. Subtracting it
 * removed a whole failure mode.
 */
