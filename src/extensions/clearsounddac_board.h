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
    AUDIOHW_CMD_VOLUME_BOOST_UPDATE,  //this is a workaround for android volume control
    AUDIOHW_CMD_MUTE_SET,             //host mute intent: {muted}     -> reg7 + LED
    AUDIOHW_CMD_MUTE_RESYNC,          //re-apply reg7 for host_muted  (after AudioHwConfig)
    AUDIOHW_CMD_LED_STATUS,           //bring-over fd9242e: {LED_ST_*} boot status
    AUDIOHW_CMD_LED_RATE,             //{samFreq, dsdMode} rate tier display
    /* Unit identity, for the diagnostic core. Appended, never inserted: this
     * enum is positional and both sides are compiled from here, so a new
     * member at the end cannot desynchronise a shipped pair.
     * Needed because the identity statics are per-tile (.dp.bss): the tile-1
     * SCPI core reading them directly sees its own zeroed copy and reports
     * "nothing read" forever. Ask the tile[0] owner instead, exactly as
     * REGister:READ? does. */
    AUDIOHW_CMD_VOL_REAPPLY,        //re-run the ONE volume applier (trim changed)
    AUDIOHW_CMD_PW_TIMEOUT_GET,     //read the persisted idle-power timeout
    AUDIOHW_CMD_PW_TIMEOUT_SAVE,    //journal (timeout, trim pair) as one record
    AUDIOHW_CMD_TRIM_LOAD,          //read the journal, publish into the mailbox
    AUDIOHW_CMD_TRIM_SAVE,          //journal the CURRENT mailbox trim pair
    AUDIOHW_CMD_TRIM_STAT,          //reply the 7 persistence status words from flash
    AUDIOHW_CMD_TRIM_RESET,         //write the default condition at the current version
    AUDIOHW_CMD_UPS_SET,            //enable/disable XMOS upsampling + DAC OSF bypass
    AUDIOHW_CMD_UPS_GET,            //reply {applied, frame clock, refusals}
    AUDIOHW_CMD_HWCFG_RATE,         //tile[1] reports the frame clock just configured
    AUDIOHW_CMD_TRIM_UPS_GET,       //tile[1] fetches the persisted upsampling intent
    AUDIOHW_CMD_IDENTITY,
    AUDIOHW_CMD_ERRLOG_RSEC,        //load one 4 KiB ring sector into the cache
    AUDIOHW_CMD_ERRLOG_ERASE,       //erase the 8 ring sectors (never sector 0)
    AUDIOHW_CMD_ERRLOG_STAT,        //reply one errlog status word by index
    AUDIOHW_CMD_ERRLOG_FMT,         //RETIRED (FMT deadlock): kept for value stability
    AUDIOHW_CMD_ERRLOG_PTR,         //RETIRED (no cross-tile RAM): kept for value stability
    AUDIOHW_CMD_ERRLOG_P0,          //post ladder: stage word 0 (ev)
    AUDIOHW_CMD_ERRLOG_P1,          //post ladder: stage word 1 (a0)
    AUDIOHW_CMD_ERRLOG_P2,          //post ladder: stage word 2 (a1)
    AUDIOHW_CMD_ERRLOG_P3,          //post ladder: stage word 3 (a2)
    AUDIOHW_CMD_ERRLOG_PGO,         //post ladder: commit staged post
    /* Kernel-update soft-ramp (owner directive 2026-11-10). The chip's own
     * glitch-free ramp does the muting: ES9219Q datasheet v1.3 "Soft Mute"
     * -- asserting Reg 7 MUTE ramps the output to -inf and clearing it ramps
     * back UP TO THE LEVEL THE VOLUME REGISTERS HOLD (Reg 3/15/16 untouched
     * by mute, so the pre-update level returns by silicon, no saved state,
     * nothing to leave mis-restored on a failed path). Rate comes from Reg 6
     * VOLUME_RATE[2:0]: dB/s = 2^rate * FS / 512 (v1.3 Register 6); rate 0
     * at the 384 kHz wire is 750 dB/s, i.e. 127.5 dB full travel in ~170 ms.
     * PRE {reply counter}: set rate 0, compose MUTE (host mute still wins),
     * wait the ramp out inside the owner task. POST {reply counter}: clear
     * MUTE (host mute still wins), restore Reg 6 to its boot rate. No-op
     * while powered down: the wake path re-applies every codec register. */
    AUDIOHW_CMD_KRNL_SEQ_PRE,
    AUDIOHW_CMD_KRNL_SEQ_POST,
    /* Two-button hold (owner request 2026-11-16): flip the codec 8x FIR
     * between linear phase (reg 7 [7:5]=000) and hybrid (110). Carried by
     * the button FSM AND the SCPI DIAG:FILTer hook; no argument, no reply.
     * Appended per the positional-enum rule at the top of this list. */
    AUDIOHW_CMD_FILTER_TOGGLE,
    /* FIR preset READBACK. The codec's I2C read path is dead on this board
     * (every register, CHIP_ID included, reads 0x00 -- measured 2026-11-16),
     * so the truth is the tile[0] owner's s_filter_bits. Reply: preset
     * value, reg 7 [7:5] (6 = hybrid, 0 = linear). Appended per the
     * positional-enum rule. */
    AUDIOHW_CMD_FILTER_STATE
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
