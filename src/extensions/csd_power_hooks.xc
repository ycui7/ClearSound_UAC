/* Idle-power wiring (TODO item 7). Owner directive 2026-11-04: after 5
 * minutes with no host audio, bring the chain down amplifier-first, and bring
 * it back DSP/mixer first, then a full codec re-init, then volume restore,
 * then the amplifier last.
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
 * WHY THE POLICY RUNS AS ITS OWN TASK ON tile[1]
 * It has to be a task on the witness tile, and it cannot be folded into the
 * audio thread or the mixer: the codec boot script it runs BLOCKS (PLL settle,
 * up to 20 chip-ID retries at 5 ms, and on a dead board a 2 s LED-bypass
 * render), and neither the audio thread nor mixer1 may ever block -- the
 * audiohub handshake is lock-step. A dedicated task with no deadline is the
 * only place a blocking re-init is free.
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

/* Takes the chanend UNSAFE, like every other user of the global power
 * channel. Passing a global unsafe chanend to a safe-parameter function needs
 * a checked demotion, and XC implements that as a runtime ecall -- measured on
 * hardware 2026-11-05: ET_ECALL at main.xc:546 in this very task, USB never
 * enumerated. Declaring the parameter unsafe removes the demotion (and the
 * check) instead of satisfying it, which is also what uc_audiohw1 does: the
 * global stays unsafe and each use site opens `unsafe { }` around it. */
/* One actuator command out to the codec server.
 *
 * Wrapped because the global power chanend is unsafe (it is bound inside the
 * spawn macro, where a per-branch cast would race -- see the note at the task
 * header), and XC requires an unsafe region to touch an unsafe resource. Same
 * shape as GPIO_WR() in clearsounddac_board.xc, which wraps its uc_audiohw1
 * sends for the same reason.
 *
 * One command word only: the five actuator commands carry no payload. Ordering
 * across calls is guaranteed by the dedicated chanend, so the down sequence
 * (VOL_MIN, AMP_OFF, DAC_OFF) and the wake sequence (VOL_RESTORE, AMP_ON) each
 * arrive in order with no handshake. */
static inline void pw_send(unsafe chanend c, unsigned cmd)
{
#ifdef CSD_PW_TRACE
    debug_printf("PWTRACE assistant: sending %u\n", cmd);
#endif
    unsafe { c <: cmd; }
#ifdef CSD_PW_TRACE
    debug_printf("PWTRACE assistant: send %u completed\n", cmd);
#endif
}

/* Takes the chanend UNSAFE, like every other user of the global power
 * channel. Passing a global unsafe chanend to a safe-parameter function needs
 * a checked demotion, and XC implements that as a runtime ecall -- measured on
 * hardware 2026-11-05: ET_ECALL at main.xc:546 in this very task, USB never
 * enumerated. Declaring the parameter unsafe removes the demotion (and the
 * check) instead of satisfying it, which is also what uc_audiohw1 does: the
 * global stays unsafe and each use site opens `unsafe { }` around it. */

void csd_PowerAssistantTask(unsafe chanend c_pw)
{
    timer t;
    unsigned now, act_seen = 0u;
    t :> now;

    csd_power_set_dsp_enabled(PCM_DSP_EN);
    csd_power_init();

    while (1) {
        select {
            case t when timerafter(now) :> void:
                now += 500u * XS1_TIMER_KHZ;        /* 0.5 s policy grid */
            {
                /* ---- witness: did any real (non-zero) host sample arrive?
                 * Counter lives on THIS tile (mixer1 writes it in lib_xua's
                 * GetSamplesFromHost), so this read is same-tile and true. */
                unsigned a = csd_pw_host_activity();
                if (a != act_seen) {
                    act_seen = a;
                    csd_power_note_frame_activity();
                }
#ifdef CSD_PW_TRACE
                {   /* DIAGNOSTIC (temporary): is the assistant alive, and
                     * where does the idle timer stall? Prints the three
                     * quantities the decision depends on. */
                    static unsigned tr = 0;
                    if ((++tr % 20u) == 1u) {          /* ~10 s at 0.5 s */
                        unsigned mn = csd_pw_dbg_word(0u), mx = csd_pw_dbg_word(1u),
                                 last = csd_pw_dbg_word(2u), dist = csd_pw_dbg_word(3u);
                        debug_printf("PWTRACE act=%u frames=%u state=%d idle=%u/%u flags=0x%x tick=%u\n",
                                     csd_pw_host_activity(), csd_pw_host_frames(),
                                     (int)csd_power_state(), csd_power_idle_ms_dbg(),
                                     (unsigned)CSD_PW_IDLE_MS, csd_power_dbg_flags(), tr);
                        debug_printf("PWTRACE words min=0x%x max=0x%x last=0x%x distinct=%u mclk=%u\n",
                                     mn, mx, last, dist, csd_pw_last_mclk());
                        csd_pw_dbg_reset();
                    }
                }
#endif
                csd_power_tick(CSD_PW_TICK_MS);

                /* One action per grid slot: a whole down/up sequence lands in
                 * <= 4 slots (2 s), invisible against a 5-minute timeout, and
                 * it keeps this case bounded -- except DAC_REINIT, which
                 * blocks here by design. */
                unsigned act = csd_power_next_action();
                switch (act) {
                    case CSD_PW_ACT_AMP_VOL_MIN:
                        /* Print BEFORE the send: with the print after, a send
                         * that never completed produced no output at all and
                         * the shutdown looked like it had never started
                         * (measured 2026-11-05: trace froze at idle=19500,
                         * exactly the tick that issues this command). */
                        debug_printf("CSD_PW: amp-down pre-step, volume to minimum\n");
                        pw_send(c_pw, CSD_PW_CMD_VOL_MIN);
                        break;
                    case CSD_PW_ACT_AMP_OFF:
                        debug_printf("CSD_PW: amplifier disabled\n");
                        pw_send(c_pw, CSD_PW_CMD_AMP_OFF);
                        break;
                    case CSD_PW_ACT_DAC_OFF:
                        debug_printf("CSD_PW: DAC muted + held in reset\n");
                        pw_send(c_pw, CSD_PW_CMD_DAC_OFF);
                        break;
                    case CSD_PW_ACT_DSP_OFF:
                        /* What this does: the biquad bypass (preset 0 =
                         * identity). The mixer MULTIPLIER cannot be stopped at
                         * runtime -- the audiohub handshake is lock-step, so
                         * stopping it is a task lifecycle change, not a
                         * register write. Claiming otherwise would be a fake;
                         * the VPU work, which is the real power cost, is gone.
                         *
                         * Posted as a LOCAL flag, not a command: the audio
                         * thread that adopts it is on THIS tile, so a same-tile
                         * static is correct here and a channel would be waste.
                         * It is not a call into csd_dsp.xc either: an
                         * &-to-XC-static inside a called XC function is
                         * attributed by the branch checker to the CALLER's par
                         * branch, and the linker then refuses the image. */
                        csd_pw_request_dsp_bypass(1u);
                        debug_printf("CSD_PW: DSP bypassed (mixer keeps running)\n");
                        break;
                    case CSD_PW_ACT_DSP_ON:
                        csd_pw_request_dsp_bypass(0u);   /* audio thread adopts */
                        debug_printf("CSD_PW: DSP restored\n");
                        break;
                    case CSD_PW_ACT_DAC_REINIT:
                        /* FULL RE-INIT BY REUSE: the SAME entry point USB boot
                         * calls (owner: "when you restart DAC, don't forget to
                         * re-initialize it"). A hand-written lighter init would
                         * drift from the boot sequence the day either changed;
                         * reuse is the honest form of "full".
                         *
                         * Blocks right here -- this task has no deadline, and
                         * the codec server must stay free to service the I2C
                         * these writes travel over.
                         *
                         * No AudioHwConfig() alongside it, deliberately: that
                         * reaches csd_dsp_set_rate, which writes the audio
                         * branch's csd_dsp statics (branch checker refuses this
                         * par branch), and lib_xua already runs the full
                         * AudioHwConfig_Mute / AudioHwConfig / _UnMute sequence
                         * itself whenever the host (re)opens a stream
                         * (xua_audiohub.xc:1013-1038) -- a precondition of ever
                         * getting here, since the wake completes only on real
                         * samples arriving. */
                        debug_printf("CSD_PW: full codec re-init\n");
                        csd_config_t cfg;
                        cfg.default_mclk = csd_pw_last_mclk();
                        csd_AudioHwInit(cfg);
                        /* Ordering guarantee: the channel preserves message
                         * order, so VOL_RESTORE below cannot overtake this
                         * boot script -- the server applies the level only
                         * after the chip has actually been re-initialised. */
                        break;
                    case CSD_PW_ACT_VOL_RESTORE:
                        debug_printf("CSD_PW: volume restored after re-init\n");
                        pw_send(c_pw, CSD_PW_CMD_VOL_RESTORE);
                        break;
                    case CSD_PW_ACT_AMP_ON:
                        /* LAST: deassert the amplifier (owner: SUSPEND_N
                         * deasserted last). */
                        debug_printf("CSD_PW: amplifier enabled\n");
                        pw_send(c_pw, CSD_PW_CMD_AMP_ON);
                        break;
                    default:
                        break;
                }
            }
            break;
        }
    }
}

/* NO alt-0 / UserAudioStreamState hook, deliberately.
 * An earlier cut added one as a FAST shutdown trigger. It is not in the
 * requirement ('no audio stream coming for more than 5 min'), and it needed a
 * cross-tile path that has only two possible shapes, both wrong: a C static
 * written on tile[0] and read on tile[1] (measured: silently reads 0), or a
 * codec-server -> assistant send (deadlock, because the codec boot script the
 * assistant runs is served I2C BY that server). The silence witness above
 * already satisfies the requirement on its own tile with no channel in the
 * trigger path at all. Subtracting it removed a whole failure mode.
 */
