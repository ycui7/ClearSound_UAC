// Copyright 2021-2024 XMOS LIMITED.
// This Software is subject to the terms of the XMOS Public Licence: Version 1.

#ifndef _USER_MAIN_H_
#define _USER_MAIN_H_


#ifdef __XC__

#include <platform.h>
#include <debug_print.h>
#include <math.h>   
#include <stdio.h>

#include <button_debounce.h>

void AudioHwRemote(chanend c[]);
#ifdef CSD_IDLE_POWER
/* Tile[1] idle-power policy task (csd_power_hooks.xc). Takes the endpoint as
 * an ARGUMENT rather than through a global chanend: an unbound chanend blocks
 * a send forever instead of faulting, which is how the first cut hung
 * (2026-11-05: 'sending 64' printed, 'send completed' never did). */
void csd_PowerAssistantTask(unsafe chanend c_pw);
#endif

extern unsafe chanend uc_audiohw1;
extern unsafe chanend uc_audiohw2;  //hardware volume control from lib_xua_mod
extern unsafe chanend uc_audiohw3;
extern unsafe chanend uc_audiohw4;
extern unsafe chanend uc_audiohw5;
//extern unsafe chanend uc_audiohw3;
//unsafe chanend uc_audiohw3;

/* Client end of the diagnostic core's codec-register channel. Its peer is
 * passed to the tile-0 register server; the two endpoints go to two different
 * tasks of the same top-level par, connected by the compiler -- the same way
 * uc_audiohw1/c_audiohw[0] already work.
 *
 * A dedicated channel (not another index of c_audiohw[]) so our request/reply
 * sequence can never interleave with the audio core's on one chanend: sharing
 * would let us read back someone else's answer, which for a diagnostic
 * interface is worse than returning an error. */
extern unsafe chanend uc_audiohw_diag;


#define SC_CLEARSOUND_DAC          1

/* csd_identity_task() is gone: the identity read is now a flash read issued
 * once from AudioHwRemoteTile0 (see clearsounddac_board.xc), so it needs no
 * core of its own. Removing the declaration alone would not free anything --
 * the par() entry below is what allocates the logical core, which is why that
 * entry is deleted too. */
#define USER_MAIN_FUNCTION_DECLARATIONS     extern void process_xscope(chanend);\
                                            extern void csd_dsp_eq_task(void);

                                            // on tile[0]: in buffered port:1 p_butt_up    = PORT_BUTTON_UP; \
                                            // on tile[0]: in buffered port:1 p_butt_down  = PORT_BUTTON_DOWN; 

/* Index 4 was never used: AudioHwRemote() forwards the array to
 * csd_AudioHwRemote(), which references only c[0], c[1], c[2], c[3] (c[2] via
 * uc_audiohw3 in USER_MAIN_CORES below). An unused chan is not free -- it still
 * allocates a chanend on each tile. tile[0] is at its 32-chanend ceiling, and
 * the lib_xua mixer's control path needs one more, which is what pushed it to
 * 33/32 and failed the xmap constraints check. Sizing the array to actual use
 * reclaims that chanend.
 *
 * Owner directive 2026-11-04 (chanend diet). Two indices retired:
 *  - [2] uc_audiohw3: xscope-era slot, ZERO send sites tree-wide (grepped).
 *    Both its ends sat on tile[0] (server select + the dead tile[0] binding
 *    below) -- the single most expensive dead byte on this tile.
 *  - [1] uc_audiohw2: ep0's volume/mute channel. ep0 and the codec server are
 *    BOTH on tile[0] (proven: the binding works under the tile-private-static
 *    rule), so this was a tile[0]->tile[0] "remote" channel. Volume is now a
 *    same-tile seqlock mailbox, csd_vol_mailbox.c; lib_xua ep0 calls
 *    AudioHwRemote_Volume_Update(ch, val) without a chanend.
 * Remaining array: [0] audio core's codec access (tile[1] client), [1] the
 * tile[1] diagnostic core's register/trim proxy. uc_audiohw_diag MUST be
 * re-pointed to the shifted slot below or it silently binds the wrong end.
 * Acceptance: xobjdump --resources tile[0] chanend bound drops by 4 (one
 * dead pair + the volume pair) and SCPI REG?/VOLume:TRIM? still answer. */
#define USER_MAIN_DECLARATIONS              chan cCsdPop, cCsdUbR;   /* dp225e: 2 chans (UbL retired — chres scarcity) */ /* dp225: channel data-plane. Passed as CHAN (not subscripts) into the two threads that need them: usb_audio_io (wire, A side) and csd_ps_thread_t1 (producer, B side) — each conversion allocates that thread's peer end (vendor spdif_tx idiom; multi-tile main forbids any other resource use). */ \
                                            chan c_audiohw[3]; \
                                            chan xscope_data_in;

// xscope_host_data(xscope_data_in); must be place before the first par statement. otherwise all sorts of build error. 
/* xscope threads DISABLED for the tile-budget experiment (2026-11-04), kept
 * commented per standing rule -- disabled config stays in the file with a
 * why-note, never deleted.
 *
 * The app spawns xscope_host_data() and process_xscope() UNCONDITIONALLY, so
 * both allocate a logical core on tile[0] in EVERY build, release included
 * (-DXSCOPE -fxscope are hardcoded at CMakeLists.txt:15,19). Neither is ever
 * configured: xscope_user_init() -- the only thing that registers or configures
 * the trace channel -- is defined in lib_xua main.xc:266 and has NO call site
 * anywhere in ~/sw-audio-sc (verified by grep). The transport code itself is
 * real (process_xscope at clearsounddac_board.xc:976 selects on
 * xscope_data_from_host and prints), so this is not removable dead code in the
 * linker sense -- it is a configured-nowhere facility still paying a scheduler
 * slot. A core that traces nothing still consumes one, and xcore.ai guarantees
 * each core only
 * 1/5 of tile MIPS at <=5 active cores, degrading to 1/n beyond. tile[0]
 * measured 7 threads (xobjdump --resources): 86 MIPS guaranteed each, not 120.
 *
 * Each also holds a chanend on tile[0], which is at its 32/32 ceiling (see the
 * c_audiohw note above), so dropping them should relieve that too -- but
 * `chan xscope_data_in` is left DECLARED on purpose: this file's own note says
 * an unused chan is not free, and `chanend` accounting is the mapper's, not
 * the declaration's. Verify freed chanends with
 *   xobjdump --resources <.xe> | grep 'channel end usage'
 * rather than assuming them. Re-enable by uncommenting both entries; each
 * costs 1 core on the most contended tile.
 *
 * SPLIT per owner directive 2026-11-04: outbound ONLY is wanted.
 *   xscope_host_data  = TARGET->HOST transport: the trace/printf stream
 *                       (xrun --io console). RE-ENABLED. Without it the
 *                       xrun log is structurally empty -- the "legitimately
 *                       empty log" seen all week was this half being off.
 *   process_xscope    = HOST->TARGET control: parses 'value+l2/l3/x' param
 *                       commands (clearsounddac_board.xc:977). STAYS OFF --
 *                       the SCPI-over-MIDI surface owns control now.
 * Cost: +1 thread on tile[0], and this task OWNS the xscope_data_in chanend
 * (a chanend belongs to one thread -- with process_xscope gone there is no
 * double-owner). Verify with xobjdump --resources, do not assume. */
#ifdef CSD_IDLE_POWER
/* Tile[1] policy task: the idle-power state machine, the stream-witness read,
 * the codec boot re-init (it blocks -- this task has no deadline) and the
 * actuator commands. +1 thread on tile[1], zero on the contended tile[0].
 *
 * A MACRO on purpose: a preprocessor directive cannot appear inside a macro
 * body, so the condition lives here with #ifdef OUTSIDE the definitions and is
 * spliced into USER_MAIN_CORES by name. (A #define whose body ends in '\' also
 * splices the following line into itself, which silently turned this into an
 * empty macro the first time -- no trailing backslash here; the continuation
 * belongs at the USE site below.)
 *
 * The endpoint is converted in the tile[1] par scope, inside unsafe{}, exactly
 * like the proven `uc_audiohw1 = (chanend) c_audiohw[0];`. No global, no bind
 * statement, so there is no way to reach the task with an unbound chanend. */
void csd_ps_thread(void);      /* dp223: tile[0] no-op shell (dp225: takes NO end — a bound peer would steal messages) */
void csd_ps_thread_t1(chanend, chanend);   /* dp223: the producer, tile[1] core; dp225: called WITH chan => peer end auto-binds */
#define CSD_PW_TILE1_ENTRY                  unsafe { csd_PowerAssistantTask((chanend) c_audiohw[2]); }
#else
#define CSD_PW_TILE1_ENTRY
#endif

/* Relocated per-frame EQ (2026-11-10): the biquad cannot live on the
 * audiohub core -- that loop GENERATES LRCLK in software (preset-0 control
 * 383,736/s at 384 kHz = zero compute margin; inline EQ collapsed it to
 * 255,830/s), and the mixer1 task is a synchronous relay inside the same
 * per-frame exchange, not a second schedule. It therefore runs as its own
 * task on a free tile[1] logical core (xobjdump: >=3 unused threads), with
 * an O(1) SPSC ring hand-off on the bit-timed side. See the relocation
 * block in csd_dsp.xc. REVERT: delete this macro and its use in
 * USER_MAIN_CORES below. */
#ifdef PCM_DSP_EN
#define CSD_EQ_TILE1_ENTRY                  csd_dsp_eq_task();
#else
#define CSD_EQ_TILE1_ENTRY
#endif
/* Inline A/B arm (CSD_DSP_INLINE_EQ): the task is not spawned -- the EQ runs
 * in the frame hook (collapsed-rate control arm). Spawning it anyway would
 * double-writer g_csd_eq_frames/-ticks_max (UBM + task) and the whole-program
 * parallel-usage analysis rejects that. */
#ifdef CSD_DSP_INLINE_EQ
#undef CSD_EQ_TILE1_ENTRY
#define CSD_EQ_TILE1_ENTRY
#endif
/* dp209 2026-11-23: wire-word producer relocated to tile[1]. The dp161
 * branch in main.xc put csd_ps_thread on tile[0], the bit-timed wire core:
 * cooperative scheduling means the producer's ~241 t of work (2 remote pops
 * of tile[1] state + UBM + publish) lands INSIDE the wire's 274 t frame
 * budget, so the measured pass cycle was wake+work-bound at 339 t regardless
 * of the servo stride (window witness tdl/256 = 339 t, stride pinned 290,
 * p_err clamped -64): a structural 19 % under-feed at 192k -> periodic
 * hold-last repeats = the 122.88 Hz comb. On tile[1] the pop state is LOCAL
 * (the feed/EQ tasks own it) and the pass no longer contends with LRCLK
 * generation. dp161's enumeration stall was from AudioHub-INTERNAL par;
 * a top-level tile[1] par entry is the proven EQ-task pattern. REVERT:
 * restore the main.xc branch and delete this macro + its use. */
/* dp209: unconditional (the board ships CSD_UPS_ASYNC-only; a guard here
 * would silently delete the only producer — dp167: EXTRA_BUILD_FLAGS never
 * reach compilation, and CSD_UPS_ASYNC is a per-.xc-file define).
 * dp209b: REMOVED again — tile[1] measured NO pass-cycle change (the pop's
 * cross-DS traffic is remote either way) and the core hosts EQ/feed bursts;
 * the thread lives in main.xc's own tile[0] branch. NEVER spawn both. */
#define CSD_PS_TILE1_ENTRY  { csd_ps_thread_t1(cCsdPop, cCsdUbR); }  /* dp225: ends allocated at main.xc par entry (same scope) */   /* dp223: producer on its OWN tile[1] core — all-LOCAL DS (queue, slot, engine), block compute never touches the wire core (owner: our code, our threads) */

#define USER_MAIN_CORES     xscope_host_data(xscope_data_in); \
                            on tile[1]: {\
                                            par\
                                            {\
                                                unsafe{\
                                                    uc_audiohw1 = (chanend) c_audiohw[0];\
                                                    uc_audiohw_diag = (chanend) c_audiohw[1];\
                                                }\
                                                CSD_PW_TILE1_ENTRY\
                                                CSD_EQ_TILE1_ENTRY\
                                                CSD_PS_TILE1_ENTRY\
                                            }\
                                        }\
    \
                            on tile[0]: {\
                                            par\
                                            {\
                                                /* uc_audiohw2/uc_audiohw3 bindings
                                                 * removed 2026-11-04 chanend diet:
                                                 * [1] volume/mute -> csd_vol_mailbox
                                                 * (ep0 and this server share tile[0]);
                                                 * [2] dead xscope-era slot, zero
                                                 * send sites. Each binding pinned a
                                                 * tile[0] chanend that no message
                                                 * ever used. */\
                                                AudioHwRemote(c_audiohw);\
                                                /* process_xscope(xscope_data_in); */ \
                                            }\
                                        }
#endif

#endif


                                                // button_debounce_task (p_butt_down,  1 ); \
                                                // button_debounce_task (p_butt_up,    0 ); \
                                                    //uc_audiohw5 = (chanend) c_audiohw[4];\
                                                    //uc_audiohw4 = (chanend) c_audiohw[3];\
