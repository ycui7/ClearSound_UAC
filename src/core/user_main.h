// Copyright 2021-2024 XMOS LIMITED.
// This Software is subject to the terms of the XMOS Public Licence: Version 1.

#ifndef _USER_MAIN_H_
#define _USER_MAIN_H_


#ifdef __XC__

#include <platform.h>
#include <debug_print.h>
#include <math.h>   
#include <stdio.h>

void AudioHwRemote(chanend c[]);

extern unsafe chanend uc_audiohw;
extern unsafe chanend uc_audiohw2;

#define SC_CLEARSOUND_DAC          1

#define USER_MAIN_DECLARATIONS      chan c_audiohw[2];

#define USER_MAIN_CORES on tile[1]: {\
                                        par\
                                        {\
                                            unsafe{\
                                                uc_audiohw = (chanend) c_audiohw[0];\
                                            }\
                                        }\
                                    }\
\
                        on tile[0]: {\
                                        par\
                                        {\
                                            unsafe{\
                                                uc_audiohw2 = (chanend) c_audiohw[1];\
                                            }\
                                            AudioHwRemote(c_audiohw);\
                                        }\
                                    }

#endif

#endif
