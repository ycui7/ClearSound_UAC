// Copyright 2024 XMOS LIMITED.
// This Software is subject to the terms of the XMOS Public Licence: Version 1.
#include <xs1.h>

#include "xassert.h"
#include "i2c.h"
#include "es9219q.h"
#include "clearsounddac_board.h"
#include <platform.h>
extern "C" {
    #include "sw_pll.h"
}

#include <debug_print.h>

// CODEC I2C lines
on tile[0]: port p_i2c_scl = PORT_I2C_SCL;
on tile[0]: port p_i2c_sda = PORT_I2C_SDA;

// CODEC reset line
on tile[0]: out port p_codec_reset  = PORT_CODEC_RST_N;

// CODEC Reset bit mask
#define CODEC_RELEASE_RESET      (0x8) // Release codec from reset


static inline void ES9219Q_REGREAD(unsigned reg, unsigned &val, client interface i2c_master_if i2c)
{
    i2c_regop_res_t result;
    val = i2c.read_reg(ES9219Q_I2C_DEVICE_ADDR, reg, result);
}

static inline void ES9219Q_REGWRITE(unsigned reg, unsigned val, client interface i2c_master_if i2c)
{
    i2c.write_reg(ES9219Q_I2C_DEVICE_ADDR, reg, val);
}

[[combinable]]
void AudioHwRemote2(chanend c, client interface i2c_master_if i2c)
{
    while(1)
    {
        select{
            case c :> unsigned cmd:
                if(cmd == AUDIOHW_CMD_REGRD)
                {
                    unsigned regAddr, regVal;
                    c :> regAddr;
                    ES9219Q_REGREAD(regAddr, regVal, i2c);
                    c <: regVal;
                }
                else if (cmd == AUDIOHW_CMD_REGWR)
                {
                    unsigned regAddr, regValue;
                    c :> regAddr;
                    c :> regValue;
                    ES9219Q_REGWRITE(regAddr, regValue, i2c);
                }
                else if (cmd ==AUDIOHW_CMD_GPIORD)
                {
                    debug_printf("AudioHwRemote_GPIO_Read_Entering\n");
                    return;
                    //  add gpio read
                    unsigned regAddr, regVal;
                    c :> regAddr;
                    ES9219Q_REGREAD(regAddr, regVal, i2c);
                    c <: regVal;
                    debug_printf("Cmd:\t%d\tPin:%d\tValue:%d\n", AUDIOHW_CMD_GPIORD, regAddr, regVal );
                }
                else if (cmd ==AUDIOHW_CMD_GPIOWR)
                {
                    debug_printf("AudioHwRemote_GPIO_Write_Entering\n");
                    // add gpio write
                    unsigned regAddr, regValue;
                    c :> regAddr;
                    c :> regValue;
                    debug_printf("Cmd:\t%d\tPin:%d\tValue:%d\n", AUDIOHW_CMD_GPIOWR, regAddr, regValue );
                    ES9219Q_REGWRITE(regAddr, regValue, i2c);
                } 
                else if (cmd == AUDIOHW_CMD_EXIT)
                {
                    i2c.shutdown();
                    return;
                }
            break;
        }
    }

}

void csd_AudioHwRemote(chanend c)
{

    i2c_master_if i2c[1];

    [[combine]]
	 par
    {
        i2c_master(i2c, 1, p_i2c_scl, p_i2c_sda, 10);
        AudioHwRemote2(c, i2c[0]);
    }

}

unsafe chanend uc_audiohw;

static inline void CODEC_REGWRITE(unsigned reg, unsigned val)
{
    unsafe
    {
        uc_audiohw <: (unsigned) AUDIOHW_CMD_REGWR;
        uc_audiohw <: reg;
        uc_audiohw <: val;
    }
}

static inline void CODEC_REGREAD(unsigned reg, unsigned &val)
{
    unsafe
    {
        uc_audiohw <: (unsigned) AUDIOHW_CMD_REGRD;
        uc_audiohw <: reg;
        uc_audiohw :> val;
    }
}

static inline void GPIO_WR(unsigned reg, unsigned val)
{
    debug_printf("AudioHwRemote_GPIO_Write_Requesting\n");
    unsafe
    {
        uc_audiohw <: (unsigned) AUDIOHW_CMD_GPIOWR;
        uc_audiohw <: reg;
        uc_audiohw <: val;
        debug_printf("Cmd:\t%d\tPin:%d\tValue:%d\n", AUDIOHW_CMD_GPIOWR, reg, val );
    }
}

static inline void GPIO_RD(unsigned reg, unsigned &val)
{
    debug_printf("AudioHwRemote_GPIO_Read_Requesting\n");
    unsafe
    {
        uc_audiohw <: (unsigned) AUDIOHW_CMD_GPIORD;
        uc_audiohw <: reg;
        uc_audiohw :> val;
        debug_printf("Cmd:\t%d\tPin:%d\tValue:%d\n", AUDIOHW_CMD_GPIORD, reg, val );
    }
}

void csd_AudioHwChanInit(chanend c)
{
    unsafe{uc_audiohw = c;}
}


/* Note this is called from tile[1] but the I2C lines to the CODEC are on tile[0]
 * use a channel to communicate CODEC reg read/writes to a remote core */
void csd_AudioHwInit(const csd_config_t &config)
{
    unsigned regVal = 0;

    /* Take CODEC out of reset */
    GPIO_WR(0x0b, regVal);

// for debuggig disable this to get through    p_codec_reset <: CODEC_RELEASE_RESET;

    delay_milliseconds(100);
/* 
    // Check we can talk to the CODEC
    CODEC_REGREAD(0x0b, regVal);

    assert(regVal == 1 && msg("CODEC reg read problem"));

    // Set register page to 0
    CODEC_REGWRITE(ES9219Q_PAGE_CTRL, 0x00);

    // Initiate SW reset (PLL is powered off as part of reset)
    CODEC_REGWRITE(ES9219Q_SW_RST, 0x01);

    // Program clock settings

    // Default is CODEC_CLKIN is from MCLK pin. Don't need to change this.
    // Power up NDAC and set to 1
    CODEC_REGWRITE(ES9219Q_NDAC, 0x81);

    // Power up MDAC and set to 4
    CODEC_REGWRITE(ES9219Q_MDAC, 0x84);

    // Power up NADC and set to 1
    CODEC_REGWRITE(ES9219Q_NADC, 0x81);

    // Power up MADC and set to 4
     CODEC_REGWRITE(ES9219Q_MADC, 0x84);

    // Program DOSR = 128
    CODEC_REGWRITE(ES9219Q_DOSR, 0x80);

    // Program AOSR = 128
    CODEC_REGWRITE(ES9219Q_AOSR, 0x80);

    // Set Audio Interface Config: I2S, 24 bits, slave mode, DOUT always driving.
    //   CODEC_REGWRITE(ES9219Q_CODEC_IF, 0x20);
    CODEC_REGWRITE(ES9219Q_CODEC_IF, 0x30);     // 32 bit mode
    // Program the DAC processing block to be used - PRB_P1
    CODEC_REGWRITE(ES9219Q_DAC_SIG_PROC, 0x01);
    // Program the ADC processing block to be used - PRB_R1
    CODEC_REGWRITE(ES9219Q_ADC_SIG_PROC, 0x01);
    // Select Page 1
    CODEC_REGWRITE(ES9219Q_PAGE_CTRL, 0x01);
    // Enable the internal AVDD_LDO:
    CODEC_REGWRITE(ES9219Q_LDO_CTRL, 0x09);
    //
    // Program Analog Blocks
    // ---------------------
    //
    // Disable Internal Crude AVdd in presence of external AVdd supply or before powering up internal AVdd LDO
    CODEC_REGWRITE(ES9219Q_PWR_CFG, 0x08);
    // Enable Master Analog Power Control
    CODEC_REGWRITE(ES9219Q_LDO_CTRL, 0x01);
    // Set Common Mode voltages: Full Chip CM to 0.9V and Output Common Mode for Headphone to 1.65V and HP powered from LDOin @ 3.3V.
    CODEC_REGWRITE(ES9219Q_CM_CTRL, 0x33);
    // Set PowerTune Modes
    // Set the Left & Right DAC PowerTune mode to PTM_P3/4. Use Class-AB driver.
    CODEC_REGWRITE(ES9219Q_PLAY_CFG1, 0x00);
    CODEC_REGWRITE(ES9219Q_PLAY_CFG2, 0x00);
    // Set ADC PowerTune mode PTM_R4.
    CODEC_REGWRITE(ES9219Q_ADC_PTM, 0x00);
    // Set MicPGA startup delay to 3.1ms
    CODEC_REGWRITE(ES9219Q_AN_IN_CHRG, 0x31);
    // Set the REF charging time to 40ms
    CODEC_REGWRITE(ES9219Q_REF_STARTUP, 0x01);
    // HP soft stepping settings for optimal pop performance at power up
    // Rpop used is 6k with N = 6 and soft step = 20usec. This should work with 47uF coupling
    // capacitor. Can try N=5,6 or 7 time constants as well. Trade-off delay vs �pop� sound.
    CODEC_REGWRITE(ES9219Q_HP_START, 0x25);
    // Route Left DAC to HPL
    CODEC_REGWRITE(ES9219Q_HPL_ROUTE, 0x08);
    // Route Right DAC to HPR
    CODEC_REGWRITE(ES9219Q_HPR_ROUTE, 0x08);
    // We are using Line input with low gain for PGA so can use 40k input R but lets stick to 20k for now.
    // Route IN2_L to LEFT_P with 20K input impedance
    CODEC_REGWRITE(ES9219Q_LPGA_P_ROUTE, 0x20);
    // Route IN2_R to LEFT_M with 20K input impedance
    CODEC_REGWRITE(ES9219Q_LPGA_N_ROUTE, 0x20);
    // Route IN1_R to RIGHT_P with 20K input impedance
    CODEC_REGWRITE(ES9219Q_RPGA_P_ROUTE, 0x80);
    // Route IN1_L to RIGHT_M with 20K input impedance
    CODEC_REGWRITE(ES9219Q_RPGA_N_ROUTE, 0x20);
    // Unmute HPL and set gain to 0dB
    CODEC_REGWRITE(ES9219Q_HPL_GAIN, 0x00);
    // Unmute HPR and set gain to 0dB
    CODEC_REGWRITE(ES9219Q_HPR_GAIN, 0x00);
    // Unmute Left MICPGA, Set Gain to 0dB.
    CODEC_REGWRITE(ES9219Q_LPGA_VOL, 0x00);
    // Unmute Right MICPGA, Set Gain to 0dB.
    CODEC_REGWRITE(ES9219Q_RPGA_VOL, 0x00);
    // Power up HPL and HPR drivers
    CODEC_REGWRITE(ES9219Q_OP_PWR_CTRL, 0x30);

    // Wait for 2.5 sec for soft stepping to take effect
    delay_milliseconds(2500);

    //
    // Power Up DAC/ADC
    // ----------------
    //
    // Select Page 0
    CODEC_REGWRITE(ES9219Q_PAGE_CTRL, 0x00);
    // Power up the Left and Right DAC Channels. Route Left data to Left DAC and Right data to Right DAC.
    // DAC Vol control soft step 1 step per DAC word clock.
    CODEC_REGWRITE(ES9219Q_DAC_CH_SET1, 0xd4);
    // Power up Left and Right ADC Channels, ADC vol ctrl soft step 1 step per ADC word clock.
    CODEC_REGWRITE(ES9219Q_ADC_CH_SET, 0xc0);
    // Unmute Left and Right DAC digital volume control
    CODEC_REGWRITE(ES9219Q_DAC_CH_SET2, 0x00);
    // Unmute Left and Right ADC Digital Volume Control.
    CODEC_REGWRITE(ES9219Q_ADC_FGA_MUTE, 0x00);
 */

    delay_milliseconds(1);

    // Set the fractional divider if used
    sw_pll_fixed_clock(config.default_mclk);

    delay_milliseconds(1);
}

/* Configures the external audio hardware for the required sample frequency.
 * See gpio.h for I2C helper functions and gpio access
 */
void csd_AudioHwConfig(unsigned samFreq, unsigned mClk, unsigned dsdMode,
    unsigned sampRes_DAC, unsigned sampRes_ADC)
{
    assert(samFreq >= 22050);

    sw_pll_fixed_clock(mClk);
}

