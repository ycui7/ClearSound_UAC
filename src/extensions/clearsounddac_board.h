// Copyright 2024 XMOS LIMITED.
// This Software is subject to the terms of the XMOS Public Licence: Version 1.

#ifndef __csd_H__
#define __csd_H__

#include <xccompat.h>


/**
 *  @brief Configuration struct type for setting the hardware profile.
 *  @var 
 */
typedef struct {
    /** xk_audio_316_mc_ab_config_t::clk_mode See xk_audio_316_mc_ab_mclk_modes_t for available clock mode options. */
    unsigned default_mclk;
} csd_config_t;


/**
 * \addtogroup clearsounddac
 *
 * API for the clearsounddac board.
 * @{
 */

/** Command enumeration for channel based commands to I2C master server on other tile.
 */
typedef enum
{
    AUDIOHW_CMD_PLLREGWR,
    AUDIOHW_CMD_REGWR,
    AUDIOHW_CMD_REGRD,
    AUDIOHW_CMD_GPIORD,
    AUDIOHW_CMD_GPIOWR,
    AUDIOHW_CMD_VOLUME_UPDATE,
    AUDIOHW_CMD_BALANCE_UPDATE,
    AUDIOHW_CMD_COMP_UPDATE,
    AUDIOHW_CMD_LED_UPDATE,
    AUDIOHW_CMD_EXIT,
    AUDIOHW_CMD_ECHO, 
    AUDIOHW_CMD_FIR_UPDATE,
    AUDIOHW_CMD_VOLUME_BOOST_UPDATE   //this is a workaround for android volume control
} audioHwCmd_t;

/** Starts an I2C master server task. Must be started *before* the tile[1] csd_AudioHwInit calls. 
 * In the background this also starts a combinable channel to interface translation task
 * so the API may be used over a channel end however it still only occupies one thread.
 * May be exited after config by sending AUDIOHW_CMD_EXIT if dynamic configuration is not required.
 *
 *  \param   c    Server side of channel connecting I2C master server and HW config functions.
 */
void csd_AudioHwRemote(chanend c[]);

typedef enum
{
    AUDIOHW_CMD_VOL_UP,
    AUDIOHW_CMD_VOL_DOWN,
    AUDIOHW_CMD_MUTE,
    AUDIOHW_CMD_VS_ON,
    AUDIOHW_CMD_VS_OFF,
    AUDIOHW_CMD2_EXIT
} audioHwCmd2_t;

/** receive commands on tile1 to change volume from tile0 commands 
 */
void csd_AudioHwRemote2(chanend c);

/** Initialises the client side channel for remote communications with I2C. Must be called on tile[1] *before* csd_AudioHwInit(). 
 *
 *  \param   c    Client side of channel connecting I2C master server and HW config functions.
 */
void csd_AudioHwChanInit(chanend c);

/** Initialises the audio hardware ready for a configuration. Must be called once *after* csd_AudioHwRemote() and csd_AudioHwChanInit().
 *
 *  \param   config     Reference to the xk_audio_316_mc_ab_config_t hardware configuration struct.
 */
void csd_AudioHwInit(const REFERENCE_PARAM(csd_config_t, config));

/** Configures the audio hardware following initialisation. This is typically called each time a sample rate or stream format change occurs.
 *
 *  \param   samFreq        The sample rate in Hertz.
 *  \param   mClk           The master clock rate in Hertz.
 *  \param   dsdMode        Controls whether the DAC is to be set into DSD mode (1) or PCM mode (0).
 *  \param   sampRes_DAC    The sample resolution of the DAC output in bits. Typically 16, 24 or 32.
 *  \param   sampRes_ADC    The sample resolution of the ADC input in bits. Typically 16, 24 or 32.
 */
void csd_AudioHwConfig(unsigned samFreq, unsigned mClk, unsigned dsdMode,
                                unsigned sampRes_DAC, unsigned sampRes_ADC);

void csd_AudioHwConfig_Mute(void);

void csd_AudioHwConfig_UnMute(void);

/**@}*/ // END: addtogroup clearsounddac

#define     THD_C2  0
#define     THD_C3  1
#define     XTLK    2
#define     CH_LEFT 0                    
#define     CH_RIGHT 1  

#endif // __csd_BOARD_H__
