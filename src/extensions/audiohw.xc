	// Copyright 2021-2024 XMOS LIMITED.
// This Software is subject to the terms of the XMOS Public Licence: Version 1.
#include "xua.h"
#include "clearsounddac_board.h"

#include <debug_print.h>


#if !(DEFAULT_FREQ >= 22050)
#error
#endif

static const csd_config_t config = {
    // mclk
    (DEFAULT_FREQ % 22050 == 0) ? MCLK_441 : MCLK_48,
};


void AudioHwRemote(chanend c[])
{
	csd_AudioHwRemote(c);
}

/* Note this is called from tile[1] but the I2C lines to the CODEC are on tile[0]
 * use a channel to communicate CODEC reg read/writes to a remote core */
void AudioHwInit()
{
	csd_AudioHwInit(config);
}

/* Configures the external audio hardware for the required sample frequency.
 * See gpio.h for I2C helper functions and gpio access
 */
void AudioHwConfig(unsigned samFreq, unsigned mClk, unsigned dsdMode, unsigned sampRes_DAC, unsigned sampRes_ADC)
{
	debug_printf("AudioHWConfig:F:%d\tMCK:%d\tDAC:%d,ADC:%d\r\n",samFreq, mClk, sampRes_DAC, sampRes_ADC );
	csd_AudioHwConfig(samFreq, mClk, dsdMode, sampRes_DAC, sampRes_ADC);
}

/**
 * @brief   User code mute audio hardware
 *
 * This function is called before AudioHwConfig() and should contain user code to mute audio hardware before a
 * sample rate change in order to reduced audible pops/clicks
 *
 *  Note, if using the application PLL of a xcore.ai device this function will be called before the master-clock is
 *  changed
 */
void AudioHwConfig_Mute(void){
	debug_printf("AudioHWConfig:Mute\r\n");
	csd_AudioHwConfig_Mute();	
}

/**
 * @brief   User code to un-mute audio hardware
 *
 * This function is called after AudioHwConfig() and should contain user code to un-mute audio hardware after a
 *  sample rate change
 */
void AudioHwConfig_UnMute(void){
	debug_printf("AudioHWConfig:UnMute\r\n");
	csd_AudioHwConfig_UnMute();
}