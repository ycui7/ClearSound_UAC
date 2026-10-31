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

#define USER_MAIN_FUNCTION_DECLARATIONS     extern void process_xscope(chanend); \
                                            extern void csd_identity_task(void);

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
                                                csd_identity_task();\
                                                process_xscope(xscope_data_in); \
                                            }\
                                        }
#endif

#endif


                                                // button_debounce_task (p_butt_down,  1 ); \
                                                // button_debounce_task (p_butt_up,    0 ); \
                                                    //uc_audiohw5 = (chanend) c_audiohw[4];\
                                                    //uc_audiohw4 = (chanend) c_audiohw[3];\
