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
                                            //unsafe {    \
                                            //    debug_printf("uc_audiohw2: 0x%x\n", (uint32_t)uc_audiohw2); \
                                            //    debug_printf("uc_audiohw: 0x%x\n", (uint32_t)uc_audiohw);  \
                                            // }  \

//static void update_dac_volume(/*chanend c_audiohw, */ int channel, int volume){
//    //split total volume into analog and digital volume. 
//    //Analog volume has priority and digital volume trims the remaining gain. 
//    int const A_RES = 512, D_RES = 128, A_RANGE = (-24 *256);
//    float AVol = 0, DVol = 0, Vol_dB = volume;
//    AVol = ceil(((Vol_dB >= A_RANGE) ? Vol_dB : A_RANGE) / A_RES) * A_RES;
//    DVol = ceil((Vol_dB - AVol) / D_RES) * D_RES;
//    //debug_printf("Ch:%d\tVol_dB:\t%x\tA:\t%x\tD:\t%x\n", channel, (int)Vol_dB, (int)AVol, (int)DVol);
//    printf("Ch:%d\tVol_dB:\t%f\tA:\t%f\tD:\t%f\n", channel, (float)Vol_dB/256, (float)AVol/256, (float)DVol/256);
//}

#endif

#endif
