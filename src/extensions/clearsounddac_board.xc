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

#define ES9219_USE_PLL_WITH_MCLK

// CODEC I2C lines
on tile[0]: port p_i2c_scl = PORT_I2C_SCL;
on tile[0]: port p_i2c_sda = PORT_I2C_SDA;

// CODEC reset line
on tile[0]: out port p_codec_reset  = PORT_CODEC_RST_N;

// CODEC Reset bit mask
#define CODEC_RELEASE_RESET      (0x30) // Release codec from reset


static inline void ES9219Q_REGREAD(unsigned reg, unsigned &val, client interface i2c_master_if i2c)
{
    i2c_regop_res_t result;
    val = i2c.read_reg(ES9219Q_I2C_DEVICE_ADDR, reg, result);
    //debug_printf("I2C_Read:\tAddr:\t0x%x\tRegAddr:\t0x%x\tValue:\t0x%x\n", ES9219Q_I2C_DEVICE_ADDR, reg, val );
}

static inline void ES9219Q_REGWRITE(unsigned reg, unsigned val, client interface i2c_master_if i2c)
{
    i2c.write_reg(ES9219Q_I2C_DEVICE_ADDR, reg, val);
    //debug_printf("I2C_Write:\tAddr:\t0x%x\tRegAddr:\t0x%x\tValue:\t0x%x\n", ES9219Q_I2C_DEVICE_ADDR, reg, val );
}

static inline void ES9219Q_PLL_REGWRITE(unsigned reg, unsigned val, client interface i2c_master_if i2c)
{
    i2c.write_reg(ES9219Q_SYNC_I2C_DEVICE_ADDR, reg, val);
    //debug_printf("I2C_Write:\tAddr:\t0x%x\tRegAddr:\t0x%x\tValue:\t0x%x\n", ES9219Q_SYNC_I2C_DEVICE_ADDR, reg, val );
}

[[combinable]]
void AudioHwRemote2(chanend c, client interface i2c_master_if i2c)
{
    while(1)
    {
        select{
            case c :> unsigned cmd:

                if (cmd == AUDIOHW_CMD_PLLREGWR)
                {
                    unsigned regAddr, regValue;
                    c :> regAddr;
                    c :> regValue;
                    ES9219Q_PLL_REGWRITE(regAddr, regValue, i2c);
                }
                else if (cmd == AUDIOHW_CMD_REGWR)
                {
                    unsigned regAddr, regValue;
                    c :> regAddr;
                    c :> regValue;
                    ES9219Q_REGWRITE(regAddr, regValue, i2c);
                }
                else if(cmd == AUDIOHW_CMD_REGRD)
                {
                    unsigned regAddr, regVal;
                    c :> regAddr;
                    ES9219Q_REGREAD(regAddr, regVal, i2c);
                    c <: regVal;
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
                    debug_printf("Cmd:\t0x%x\tPin:0x%x\tValue:0x%x\n", AUDIOHW_CMD_GPIORD, regAddr, regVal );
                }
                else if (cmd ==AUDIOHW_CMD_GPIOWR)
                {
                    debug_printf("AudioHwRemote_GPIO_Write_Entering\n");
                    // add gpio write
                    unsigned regAddr, regValue;
                    c :> regAddr;
                    c :> regValue;
                    debug_printf("Cmd:\t0x%x\tPin:0x%x\tValue:0x%x\n", AUDIOHW_CMD_GPIOWR, regAddr, regValue );
                    p_codec_reset <: regValue;
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
        i2c_master(i2c, 1, p_i2c_scl, p_i2c_sda, 400);
        AudioHwRemote2(c, i2c[0]);
    }

}

unsafe chanend uc_audiohw;

static inline void DAC_PLL_REGWRITE(unsigned reg, unsigned val)
{
    unsafe
    {
        uc_audiohw <: (unsigned) AUDIOHW_CMD_PLLREGWR;
        uc_audiohw <: reg;
        uc_audiohw <: val;
        debug_printf("PLL_WRITE\tAddr: 0x%x\t(%d)\tValue: 0x%x\n", reg, reg, val );

    }
}

static inline void DAC_REGWRITE(unsigned reg, unsigned val)
{
    unsafe
    {
        uc_audiohw <: (unsigned) AUDIOHW_CMD_REGWR;
        uc_audiohw <: reg;
        uc_audiohw <: val;
        //debug_printf("I2C_WRITE\tAddr: 0x%x\t(%d)\tValue: 0x%x\t(%d)\n", reg, reg, val, val );
    }
}

static inline void DAC_REGREAD(unsigned reg, unsigned &val)
{
    unsafe
    {
        uc_audiohw <: (unsigned) AUDIOHW_CMD_REGRD;
        uc_audiohw <: reg;
        uc_audiohw :> val;
        //debug_printf("I2C_READ\tAddr: 0x%x\t(%d)\tValue: 0x%x\t(%d)\n", reg, reg, val, val );

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
        debug_printf("GPIO_WR\tPin: 0x%x\tValue: 0x%x\n", reg, val );
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
        debug_printf("GPIO_RD\tPin: 0x%x\tValue: 0x%x\n", reg, val );
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
    uint8_t AnalogVolume = 0; 
    uint8_t UserVolummeL = 0, UserVolummeR = 0;  //0.5dB steps
    int16_t ThdCompC2Ch1 = 0, ThdCompC3Ch1 = 0; 
    int16_t ThdCompC2Ch2 = 0, ThdCompC3Ch2 = 0; 
    int16_t CrosstalkCompCh1 = 0, CrosstalkCompCh2 = 0; //0x0001 is -126dB 
    uint32_t VolumeMasterTrim = 0x7FFFFFFF;
    
    debug_printf("=====================================================\n");
    
    delay_milliseconds(1);

    // Set the fractional divider if used
    sw_pll_fixed_clock(config.default_mclk);
    debug_printf("sw_pll_fixed_clock_Done\n");
    delay_milliseconds(1);

    /* Take CODEC out of reset */
    GPIO_WR(0x0b, CODEC_RELEASE_RESET);
    delay_milliseconds(1);
#ifdef ES9219_USE_PLL_WITH_MCLK    
    // start up sequence per ESS application note
    DAC_PLL_REGWRITE    (201 ,   0x19); //Set DAC Clock input to MCLK
    DAC_PLL_REGWRITE    (193 ,   0xC0); //Turn On PLL Charge Pump and VCo
    DAC_PLL_REGWRITE    (194 ,   0x00); //Set Clock_IN_DIV = 8 and FB_DIV = 131072
    DAC_PLL_REGWRITE    (195 ,   0x00); 
    DAC_PLL_REGWRITE    (196 ,   0x82); 
    DAC_PLL_REGWRITE    (197 ,   0x00); 
    DAC_PLL_REGWRITE    (198 ,   0x02); //Set Clock_OUT_Div = 2, and PFE_DELAY to 1.5ns
    DAC_PLL_REGWRITE    (199 ,   0xC2); 
    DAC_PLL_REGWRITE    (200 ,   0x0C); //Turn On PLL regulators as final step
    debug_printf("CLOCK CONFIGURATION: \"MCLK => PLL => SYSTEM CLOCK\"\n");
    delay_milliseconds(1);
#endif
        
    // Check we can talk to the DAC
    DAC_REGREAD(0x40, regVal);
    assert(regVal != 0 && msg("DAC Chip ID Register Read Problem"));
    debug_printf("CHIPI_ID:\t0x%x\tAUTOMUTE:\t0x%x\tDPLL_LOCK:\t0x%x\n", (regVal >>2), (regVal & 0x03), (regVal & 0x01));

    for (unsigned reg = 0 ; reg < 61 ; reg++ ){
        DAC_REGREAD(reg, regVal);
        debug_printf("I2C_READ\tAddr: 0x%x\t(%d)\tValue: 0x%x\t(%d)\n", reg, reg, regVal, regVal );

    }
    
    // General Comment:
    // XMOS's I2C lib is very low overhead on physical layer that there is no need to batch send I2C commands. 

    DAC_REGWRITE    (ES9219Q_AMP_CONFIG,                    (uint8_t)(ES9219Q_AMP_PDB_SS | ES9219Q_AMP_MODE) ); 
    DAC_REGWRITE    (ES9219Q_ANALOG_VOL_CTRL,               ((0b010 << 5) | (AnalogVolume & 0x1F)));
    DAC_REGWRITE    (ES9219Q_GPIO12_CONFIG,                 ((3 << 4) | 1) ); //GPIO2 CLK out, GPIO1 lock status
    DAC_REGWRITE    (ES9219Q_VOL_CTRL_LO,                   UserVolummeL ); //GPIO2 CLK out, GPIO1 lock status
    DAC_REGWRITE    (ES9219Q_VOL_CTRL_HI,                   UserVolummeR ); //GPIO2 CLK out, GPIO1 lock status
    DAC_REGWRITE    (ES9219Q_DOP_N_VOL_RAMP_RATE,           ((7 << 4) | (1<<3) | 0b010) );
    //DAC_REGWRITE    (ES9219Q_MASTER_MODE_N_SYNC_CONFIG,     ((0b01 << 5) | (0 << 4) || 2) ); //DATA_CLK = MCLK/4 for 192KHz, Disable MCLK = 128FS, DPLL 5461 FS edge Lock. 
    //DAC_REGWRITE    (ES9219Q_FILTER_SHAPE_N_SYSTEM_MUTE,    ((0b000 << 5) | 0) );
    //DAC_REGWRITE    (ES9219Q_MASTER_TRIM_BYTE0,             (uint8_t)((VolumeMasterTrim >> 0) & 0xFF) ); 
    //DAC_REGWRITE    (ES9219Q_MASTER_TRIM_BYTE1,             (uint8_t)((VolumeMasterTrim >> 1) & 0xFF) ); 
    //DAC_REGWRITE    (ES9219Q_MASTER_TRIM_BYTE2,             (uint8_t)((VolumeMasterTrim >> 2) & 0xFF) ); 
    //DAC_REGWRITE    (ES9219Q_MASTER_TRIM_BYTE3,             (uint8_t)((VolumeMasterTrim >> 3) & 0xFF) ); 
    
    //DAC_REGWRITE    (12, 0);  //disable dpll
    
    //DAC_REGWRITE    (ES9219Q_GENERAL_CONFIG,              TBD ); 
    //DAC_REGWRITE    (ES9219Q_GPIO_CONFIG_N_AUTO_CLK_GEAR, TBD ); 
    DAC_REGWRITE    (ES9219Q_CHARGE_PUMP_CLOCK_CONFIG_LO,   (uint8_t)(ES9219Q_CP_CLK_DIV & 0xFF)); 
    DAC_REGWRITE    (ES9219Q_CHARGE_PUMP_CLOCK_CONFIG_HI,   (uint8_t)(ES9219Q_CP_CLK_SEL | ES9219Q_CP_CLK_EN | ((ES9219Q_CP_CLK_DIV >> 8) & 0xFF)) ); 
    
    //DAC_REGWRITE    (ES9219Q_THD_BYPASS_N_MONO_MODE,      ( (0 << 5) | 0 ) )); // Enable THD Compensation
    //DAC_REGWRITE    (ES9219Q_THD_COMP_C2_LO,                (uint8_t)((ThdCompC2Ch1> 0) & 0xFF) ); 
    //DAC_REGWRITE    (ES9219Q_THD_COMP_C2_HI,                (uint8_t)((ThdCompC2Ch1> 1) & 0xFF) ); 
    //DAC_REGWRITE    (ES9219Q_THD_COMP_C3_LO,                (uint8_t)((ThdCompC3Ch1 >> 0) & 0xFF) ); 
    //DAC_REGWRITE    (ES9219Q_THD_COMP_C3_HI,                (uint8_t)((ThdCompC3Ch1 >> 1) & 0xFF) ); 
    //DAC_REGWRITE    (ES9219Q_THD_COMP_C2_CH2_LO,            (uint8_t)((ThdCompC2Ch2 >> 0) & 0xFF) ); 
    //DAC_REGWRITE    (ES9219Q_THD_COMP_C2_CH2_HI,            (uint8_t)((ThdCompC2Ch2 >> 1) & 0xFF) ); 
    //DAC_REGWRITE    (ES9219Q_THD_COMP_C3_CH2_LO,            (uint8_t)((ThdCompC3Ch2 >> 0) & 0xFF) ); 
    //DAC_REGWRITE    (ES9219Q_THD_COMP_C3_CH2_HI,            (uint8_t)((ThdCompC3Ch2 >> 1) & 0xFF) ); 
    
    //DAC_REGWRITE    (ES9219Q_CROSSTALK_COMP_CONFIG,         (uint8_t)(ES9219Q_BYPASS_CT | ES9219Q_ENABLE_PLL_LOCK ) ); 
    //DAC_REGWRITE    (ES9219Q_CROSSTALK_COMP_SCALE_CH1_LO,   (uint8_t)((CrosstalkCompCh1 >> 0) & 0xFF ) ); 
    //DAC_REGWRITE    (ES9219Q_CROSSTALK_COMP_SCALE_CH1_HI,   (uint8_t)((CrosstalkCompCh1 >> 1) & 0xFF ) ); 
    //DAC_REGWRITE    (ES9219Q_CROSSTALK_COMP_SCALE_CH2_LO,   (uint8_t)((CrosstalkCompCh2 >> 0) & 0xFF ) ); 
    //DAC_REGWRITE    (ES9219Q_CROSSTALK_COMP_SCALE_CH2_HI,   (uint8_t)((CrosstalkCompCh2 >> 1) & 0xFF ) ); 
    
    //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_N_I2S_MON_CONFIG,  (uint8_t)(ES9219Q_DISALBE_ATR_CH2 | ES9219Q_DISALBE_ATR_CH1 | ES9219Q_PDB_ATR_R | ES9219Q_PDB_ATR_L) );     
    //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_OVERRIDE_1,        (uint8_t)(ES9219Q_CPH_APDB) );     
    //delay_milliseconds(1);
    //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_OVERRIDE_1,        (uint8_t)(ES9219Q_CPH_APDB | ES9219Q_ENAUX | ES9219Q_AREG_PDB | ES9219Q_ENPHA | ES9219Q_CPH_ENS | ES9219Q_CPH_ENW | ES9219Q_CP_CLKIO_SEL) );     
    //delay_milliseconds(1);
    //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_OVERRIDE_2,        (uint8_t)(ES9219Q_DIG_OVER_EN | ES9219Q_SEL1V | ES9219Q_SHTOUTB | ES9219Q_SHTINB) );     
    //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_OVERRIDE_3,        (uint8_t)(ES9219Q_ENFCB | ES9219Q_ENCP_OE | ES9219Q_ENAUX_OE | ES9219Q_CPL_ENS | ES9219Q_CPL_ENW | ES9219Q_SEL3V3_PS | ES9219Q_ENSM_PS | ES9219Q_SEL3V3_CPH ) );     
    //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_SIGNALS,           (uint8_t)((1 << 6) | (1 << 3) | (1 << 2) | 1) ); 
    delay_milliseconds(10);
    //DAC_REGWRITE(1,0b10000000);

    for (unsigned reg = 0 ; reg < 61 ; reg++ ){
        DAC_REGREAD(reg, regVal);
        debug_printf("I2C_READ\tAddr: 0x%x\t(%d)\tValue: 0x%x\t(%d)\n", reg, reg, regVal, regVal );

    }
}

/* Configures the external audio hardware for the required sample frequency.
 * See gpio.h for I2C helper functions and gpio access
 */
void csd_AudioHwConfig(unsigned samFreq, unsigned mClk, unsigned dsdMode,
    unsigned sampRes_DAC, unsigned sampRes_ADC)
{
    
    assert(samFreq >= 22050);
    debug_printf("=================Clock Change===============\n");   
    sw_pll_fixed_clock(mClk);
    debug_printf("sw_pll_fixed_clock_Done\n");
    delay_milliseconds(10);
    {
        unsigned regVal = 0;
        debug_printf("===================Current State===============\n");   

        DAC_REGREAD(ES9219Q_GPIO_READBACK, regVal);
        debug_printf("\033[42m\tCLK_GEAR: %d\tGPIO2: %d\tGPIO1: %d\t\033[0m\n", (regVal>>2)&3, (regVal>>1)&1, (regVal>>0)&1);   
        
        DAC_REGREAD(ES9219Q_READ_INPUT_SEL_N_AUTOMUTE_STAT, regVal);        
        debug_printf("\033[42m\tOC_R: %d\tOC_L: %d\tAUTOMUTE_R: %d\tAUTOMUTE_L: %d\t\033[0m\n", (regVal>>7)&1, (regVal>>6)&1, (regVal>>5)&1, (regVal>>4)&1);   
        
        DAC_REGREAD(ES9219Q_READ_LOCK_STATUS, regVal);
        debug_printf("\033[42m\tMQA_LOCK: %d\tPLL_LOCK: %d\tASRC: %d\t\033[0m\n", (regVal>>2)&1, (regVal>>1)&1, (regVal)&1);   
        
        DAC_REGREAD(ES9219Q_CHIP_STATUS, regVal);
        debug_printf("\033[42m\tCHIPI_ID: %d\tAUTOMUTE: %d\tDPLL_LOCK: %d\t\033[0m\n", (regVal >>2), (regVal & 0x03), (regVal & 0x01));   
    
        //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_N_I2S_MON_CONFIG,  (uint8_t)(ES9219Q_DISALBE_ATR_CH2 | ES9219Q_DISALBE_ATR_CH1 | ES9219Q_PDB_ATR_R | ES9219Q_PDB_ATR_L) );     
        //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_OVERRIDE_1,        (uint8_t)(ES9219Q_CPH_APDB) );     
        //delay_milliseconds(1);
        //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_OVERRIDE_1,        (uint8_t)(ES9219Q_CPH_APDB | ES9219Q_ENAUX | ES9219Q_AREG_PDB | ES9219Q_ENPHA | ES9219Q_CPH_ENS | ES9219Q_CPH_ENW | ES9219Q_CP_CLKIO_SEL) );     
        //delay_milliseconds(1);
        //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_OVERRIDE_2,        (uint8_t)(ES9219Q_DIG_OVER_EN | ES9219Q_SEL1V | ES9219Q_SHTOUTB | ES9219Q_SHTINB) );     
        //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_OVERRIDE_3,        (uint8_t)(ES9219Q_ENFCB | ES9219Q_ENCP_OE | ES9219Q_ENAUX_OE | ES9219Q_CPL_ENS | ES9219Q_CPL_ENW | ES9219Q_SEL3V3_PS | ES9219Q_ENSM_PS | ES9219Q_SEL3V3_CPH ) );     
        //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_SIGNALS,           (uint8_t)((1 << 6) | (1 << 3) | (1 << 2) | 1) ); 
        //DAC_REGWRITE    (ES9219Q_AMP_CONFIG,                    (uint8_t)(ES9219Q_AMP_PDB_SS | ES9219Q_AMP_MODE_GPIO) ); 
        //delay_milliseconds(10);
        //DAC_REGWRITE(1,0b10000000);

        for (unsigned reg = 0 ; reg < 61 ; reg++ ){
            DAC_REGREAD(reg, regVal);
            debug_printf("I2C_READ\tAddr: 0x%x\t(%d)\tValue: 0x%x\t(%d)\n", reg, reg, regVal, regVal );
        }   
    }

}

