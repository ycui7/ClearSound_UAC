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
#define USER_MAIN_FUNCTION_DECLARATIONS     extern void process_xscope(chanend);

                                            // on tile[0]: in buffered port:1 p_butt_up    = PORT_BUTTON_UP; \
                                            // on tile[0]: in buffered port:1 p_butt_down  = PORT_BUTTON_DOWN; 

/* Index 4 was never used: AudioHwRemote() forwards the array to
 * csd_AudioHwRemote(), which references only c[0], c[1], c[2], c[3] (c[2] via
 * uc_audiohw3 in USER_MAIN_CORES below). An unused chan is not free -- it still
 * allocates a chanend on each tile. tile[0] is at its 32-chanend ceiling, and
 * the lib_xua mixer's control path needs one more, which is what pushed it to
 * 33/32 and failed the xmap constraints check. Sizing the array to actual use
 * reclaims that chanend. */
#define USER_MAIN_DECLARATIONS              chan c_audiohw[4]; \
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
#define USER_MAIN_CORES     xscope_host_data(xscope_data_in); \
                            on tile[1]: {\
                                            par\
                                            {\
                                                unsafe{\
                                                    uc_audiohw1 = (chanend) c_audiohw[0];\
                                                    uc_audiohw_diag = (chanend) c_audiohw[3];\
                                                }\
                                            }\
                                        }\
    \
                            on tile[0]: {\
                                            par\
                                            {\
                                                unsafe{\
                                                    uc_audiohw2 = (chanend) c_audiohw[1];\
                                                    uc_audiohw3 = (chanend) c_audiohw[2];\
                                                }\
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
