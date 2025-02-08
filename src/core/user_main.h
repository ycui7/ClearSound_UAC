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


#define SC_CLEARSOUND_DAC          1

// on tile[0]: in buffered port:1 p_butt_up    = PORT_BUTTON_UP; 
// on tile[0]: in buffered port:1 p_butt_down  = PORT_BUTTON_DOWN; 

#define USER_MAIN_FUNCTION_DECLARATIONS     extern void process_xscope(chanend); 

                                            // on tile[0]: in buffered port:1 p_butt_up    = PORT_BUTTON_UP; \
                                            // on tile[0]: in buffered port:1 p_butt_down  = PORT_BUTTON_DOWN; 

#define USER_MAIN_DECLARATIONS              chan c_audiohw[5]; \
                                            chan xscope_data_in;

// xscope_host_data(xscope_data_in); must be place before the first par statement. otherwise all sorts of build error. 
#define USER_MAIN_CORES     xscope_host_data(xscope_data_in); \
                            on tile[1]: {\
                                            par\
                                            {\
                                                unsafe{\
                                                    uc_audiohw1 = (chanend) c_audiohw[0];\
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
                                                    uc_audiohw5 = (chanend) c_audiohw[4];\
                                                    uc_audiohw4 = (chanend) c_audiohw[3];\
                                                }\
                                                AudioHwRemote(c_audiohw);\
                                                process_xscope(xscope_data_in); \
                                            }\
                                        }
#endif

#endif


                                                // button_debounce_task (p_butt_down,  1 ); \
                                                // button_debounce_task (p_butt_up,    0 ); \