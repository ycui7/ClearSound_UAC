// Copyright 2024 XMOS LIMITED.
// This Software is subject to the terms of the XMOS Public Licence: Version 1.

#include <xs1.h>
#include "xassert.h"
#include "i2c.h"
#include <platform.h>
extern "C" {
    #include "sw_pll.h"
}

#include "es9219q.h"
#include "clearsounddac_board.h"
#include "button_debounce.h"

#include <debug_print.h>
#include <math.h>
#include <stdio.h>
#include <xscope.h>
#include <print.h>
#include <stdlib.h>

//#define SC_COLOR_DEBUG_OUTPUT

#ifdef SC_COLOR_DEBUG_OUTPUT
#define ANSI_RED        "\x1b[31m"
#define ANSI_GREEN      "\x1b[32m"
#define ANSI_YELLOW     "\x1b[33m"
#define ANSI_BLUE       "\x1b[34m"
#define ANSI_MAGENTA    "\x1b[35m"
#define ANSI_CYAN       "\x1b[36m"
#define ANSI_RESET      "\x1b[0m"
#else
#define ANSI_RED        ""
#define ANSI_GREEN      ""
#define ANSI_YELLOW     ""
#define ANSI_BLUE       ""
#define ANSI_MAGENTA    ""
#define ANSI_CYAN       ""
#define ANSI_RESET      ""
#endif

#define ES9219_USE_PLL_WITH_MCLK

// might be better to change all these color to macro
unsigned const  LED_DSD_Mode = 0,
                LED_samFreq = 1, 
                LED_Volume = 2;
unsigned const  led_Red = 0, 
                led_Orange = 1,
                led_Green = 2,
                led_Blue = 3,
                led_Purple = 4,
                led_White = 5;

// DAC I2C lines
on tile[0]: port p_i2c_scl = PORT_I2C_SCL;
on tile[0]: port p_i2c_sda = PORT_I2C_SDA;

on tile[0]: in buffered port:1 p_butt_up    = PORT_BUTTON_UP; 
on tile[0]: in buffered port:1 p_butt_down  = PORT_BUTTON_DOWN; 

// DAC AMP reset line
on tile[0]: out port p_codec_reset  = PORT_CODEC_RST_N;
on tile[0]: out port p_led_024      = XS1_PORT_4F;
on tile[0]: out port p_led_35       = XS1_PORT_4E;
// on tile[0]: out port p_led_1        = XS1_PORT_8D;


// CODEC Reset bit mask
#define CODEC_RELEASE_RESET      (0x30) // Release codec from reset

static inline void ES9219Q_REGREAD(unsigned reg, unsigned &val, client interface i2c_master_if i2c)
{
    i2c_regop_res_t result;
    val = i2c.read_reg(ES9219Q_I2C_DEVICE_ADDR, reg, result);
}

static inline void ES9219Q_REGWRITE(unsigned reg, unsigned val, client interface i2c_master_if i2c)
{
    i2c.write_reg(ES9219Q_I2C_DEVICE_ADDR, reg, val);
}

static inline void ES9219Q_PLL_REGWRITE(unsigned reg, unsigned val, client interface i2c_master_if i2c)
{
    i2c.write_reg(ES9219Q_SYNC_I2C_DEVICE_ADDR, reg, val);
}

[[combinable]] void AudioHwRemoteTile0(chanend c[n], unsigned n, client interface i2c_master_if i2c)
{
    while(1)
    {
        select{
            case c[int i] :> unsigned cmd:
                if (cmd == AUDIOHW_CMD_PLLREGWR)
                {
                    unsigned regAddr, regValue;
                    c[i] :> regAddr;
                    c[i] :> regValue;
                    ES9219Q_PLL_REGWRITE(regAddr, regValue, i2c);
                }
                else if (cmd == AUDIOHW_CMD_REGWR)
                {
                    unsigned regAddr, regValue;
                    c[i] :> regAddr;
                    c[i] :> regValue;
                    ES9219Q_REGWRITE(regAddr, regValue, i2c);
                }
                else if(cmd == AUDIOHW_CMD_REGRD)
                {
                    unsigned regAddr, regVal;
                    c[i] :> regAddr;
                    ES9219Q_REGREAD(regAddr, regVal, i2c);
                    c[i] <: regVal;
                }
                else if (cmd == AUDIOHW_CMD_GPIORD)
                {
                    unsigned bank, value;
                    c[i] :> bank;
                    //  add gpio read
                    c[i] <: value;
                    return;
                    debug_printf("Cmd:\t0x%x\tPin:0x%x\tValue:0x%x\n", AUDIOHW_CMD_GPIORD, bank, value );
                }
                else if (cmd == AUDIOHW_CMD_GPIOWR)
                {
                    unsigned bank, value;
                    static unsigned reset_cache = 0, led_cache = 0;
                    c[i] :> bank;
                    c[i] :> value;

                    if       ( 0 == bank ){
                        p_codec_reset <: (reset_cache = value) ; 
                        //p_codec_reset <: (reset_cache = value & 0x30) & (led_cache) ;                         
                    }else if ( 1 == bank ){
                        //p_codec_reset <: (reset_cache) & (led_cache = value | 0xbf) ;
                    }else if ( 2 == bank ){
                        p_led_35  <: value & 0x0c;      // to gurard the other bits.
                    }else if ( 3 == bank ){
                        p_led_024 <: value & 0x0e;      // to gurard the other bits.
                    }
                    debug_printf("Cmd:\t0x%x\tPin:0x%x\tValue:0x%x\n", AUDIOHW_CMD_GPIOWR, bank, value );
                } 
                else if (cmd == AUDIOHW_CMD_COMP_UPDATE)
                {
                    unsigned channel, item;
                    int value;
                    c[i] :> channel;    
                    c[i] :> item;
                    c[i] :> value;
                    unsigned RegAddrLo, RegAddrHi;                  
                    RegAddrLo = 
                        (   item == THD_C2  ?   ( channel == CH_LEFT ? ES9219Q_THD_COMP_C2_LO               : ES9219Q_THD_COMP_C2_CH2_LO)           : 
                            item == THD_C3  ?   ( channel == CH_LEFT ? ES9219Q_THD_COMP_C3_LO               : ES9219Q_THD_COMP_C3_CH2_LO)           :
                            item == XTLK    ?   ( channel == CH_LEFT ? ES9219Q_CROSSTALK_COMP_SCALE_CH1_LO  : ES9219Q_CROSSTALK_COMP_SCALE_CH2_LO ) : 
                                                (unsigned)0
                        );
                    RegAddrHi = 
                        (   item == THD_C2  ?   ( channel == CH_LEFT ? ES9219Q_THD_COMP_C2_HI               : ES9219Q_THD_COMP_C2_CH2_HI)           : 
                            item == THD_C3  ?   ( channel == CH_LEFT ? ES9219Q_THD_COMP_C3_HI               : ES9219Q_THD_COMP_C3_CH2_HI)           : 
                            item == XTLK    ?   ( channel == CH_LEFT ? ES9219Q_CROSSTALK_COMP_SCALE_CH1_HI  : ES9219Q_CROSSTALK_COMP_SCALE_CH2_HI ) : 
                                                (unsigned)0
                        );
                        
                    if (NULL == RegAddrLo || NULL == RegAddrHi )  
                        break;

                    ES9219Q_REGWRITE(RegAddrLo, (uint8_t)((value> 0) & 0xFF), i2c);
                    ES9219Q_REGWRITE(RegAddrHi, (uint8_t)((value> 1) & 0xFF), i2c);
                    
                }
                else if (cmd == AUDIOHW_CMD_VOLUME_UPDATE)
                {
                    unsigned channel;
                    int valueA, valueDL, valueDR;
                    c[i] :> channel;    
                    c[i] :> valueA;
                    c[i] :> valueDL;
                    valueDR = valueDL;
                    //printf("Vol_Update:\tvalueA: %2.2f\tvalueD_L: %2.2f\tvalueD_R:%2.2f\n", (float)(valueA)/256, (float)(valueDL)/256, (float)(valueDR)/256 );
                    unsigned regAddrA, regValueA, regAddrDL, regValueDL, regAddrDR, regValueDR;
                    regValueA = ((-valueA) >> 8) & 0x1F;
                    regValueDL = (-valueDL) >> 7;
                    regValueDR = (-valueDR) >> 7;
                        
                    ES9219Q_REGWRITE(ES9219Q_ANALOG_VOL_CTRL,   ((0b010 << 5) | (regValueA & 0x1F)),    i2c );
                    ES9219Q_REGWRITE(ES9219Q_VOL_CTRL_LO,       regValueDL,                             i2c );           
                    ES9219Q_REGWRITE(ES9219Q_VOL_CTRL_HI,       regValueDR,                             i2c ); 
                    
                    //debug_printf("Vol_Update:\tAVOL: %d\tDVOL_L: %d\tVDOL_R: %d\n", regValueA, regValueDL, regValueDR );
                }
                else if (cmd == AUDIOHW_CMD_LED_UPDATE)
                {
                    // unsigned led, color;
                    // c[i] :> led;
                    // c[i] :> color;
                    // AudioHwRemote_LED_Update(led, color);
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

void csd_AudioHwRemote(chanend c[])
{
    i2c_master_if i2c[1];
    
    par {
        [[combine]] par
        {
            i2c_master(i2c, 1, p_i2c_scl, p_i2c_sda, 400);
            AudioHwRemoteTile0(c, 2, i2c[0]);
            button_debounce_task (p_butt_up, 0 );
            button_debounce_task (p_butt_down, 1 );
        }
    }
}

unsafe chanend uc_audiohw1;
// unsafe chanend uc_audiohw2;  //i2c hardware volume control from lib_xua
unsafe chanend uc_audiohw3;
unsafe chanend uc_audiohw4;
unsafe chanend uc_audiohw5;

static inline void DAC_PLL_REGWRITE(unsigned reg, unsigned val)
{
    unsafe
    {
        uc_audiohw1 <: (unsigned) AUDIOHW_CMD_PLLREGWR;
        uc_audiohw1 <: reg;
        uc_audiohw1 <: val;
        //debug_printf("PLL_WRITE\tAddr: 0x%x\t(%d)\tValue: 0x%x\n", reg, reg, val );
    }
}

static inline void DAC_REGWRITE(unsigned reg, unsigned val)
{
    unsafe
    {
        uc_audiohw1 <: (unsigned) AUDIOHW_CMD_REGWR;
        uc_audiohw1 <: reg;
        uc_audiohw1 <: val;
        //debug_printf("I2C_WRITE\tAddr: 0x%x\t(%d)\tValue: 0x%x\t(%d)\n", reg, reg, val, val );
    }
}


static inline void DAC_REGREAD(unsigned reg, unsigned &val)
{
    unsafe
    {
        uc_audiohw1 <: (unsigned) AUDIOHW_CMD_REGRD;
        uc_audiohw1 <: reg;
        uc_audiohw1 :> val;
        //debug_printf("I2C_READ\tAddr: 0x%x\t(%d)\tValue: 0x%x\t(%d)\n", reg, reg, val, val );
    }
}

static inline void GPIO_WR(unsigned bank, unsigned val)
{
    //debug_printf("AudioHwRemote_GPIO_Write_Requesting\n");
    unsafe
    {
        uc_audiohw1 <: (unsigned) AUDIOHW_CMD_GPIOWR;
        uc_audiohw1 <: bank;
        uc_audiohw1 <: val;
        debug_printf("GPIO_WR\tPin: 0x%x\tValue: 0x%x\n", bank, val );
    }
}

static inline void GPIO_RD(unsigned reg, unsigned &val)
{
    //debug_printf("AudioHwRemote_GPIO_Read_Requesting\n");
    unsafe
    {
        uc_audiohw1 <: (unsigned) AUDIOHW_CMD_GPIORD;
        uc_audiohw1 <: reg;
        uc_audiohw1 :> val;
        debug_printf("GPIO_RD\tPin: 0x%x\tValue: 0x%x\n", reg, val );
    }
}

void csd_AudioHwChanInit(chanend c)
{
    unsafe{uc_audiohw1 = c;}
}

/*  split total volume into analog and digital volume. 
 *  Analog volume has priority and digital volume trims the remaining gain. 
 */
{int, int} ADVolume_Split(int channel, int volume){
    int const A_RES = 512, D_RES = 128, A_RANGE = (-24 *256);
    float AVol = 0, DVol = 0, Vol_dB = volume;
    AVol = ceil(((Vol_dB >= A_RANGE) ? Vol_dB : A_RANGE) / A_RES) * A_RES;
    DVol = ceil((Vol_dB - AVol) / D_RES) * D_RES;
    //printf("Ch:%d\tVol_dB:\t%2.2f\tA:\t%2.2f\tD:\t%2.2f\n", channel, (float)Vol_dB/256, (float)AVol/256, (float)DVol/256);
    return {(int)AVol, (int)DVol};
}

static inline void  AudioHwRemote_LED_Update(unsigned led, unsigned color){
    static unsigned led_status = 0xffffffff;
    unsigned led_bits;
    if (led == LED_DSD_Mode){
    led_bits = ~(   color == led_Red     ?  0           :
                    color == led_Orange  ?  0           :
                    color == led_Green   ?  ( 1 << 23 ) :
                    color == led_Blue    ?  0           :
                    color == led_Purple  ?  0           :
                                            0 );
    }else if (led == LED_samFreq){
    led_bits = ~(   color == led_Red     ?  ( 1 << 3 )              :
                    color == led_Orange  ?  ((1 << 3) | (1 << 11))  :
                    color == led_Green   ?  ( 1 << 11 )             :
                    color == led_Blue    ?  ( 1 << 2 )              :
                    color == led_Purple  ?  ((1 << 3) | (1 << 2))   :
                                            0 );
    }else if (led == LED_Volume){
    led_bits = ~(   color == led_Red     ?  ( 1  << 8 )             :
                    color == led_Orange  ?  ((1 << 8) | (1 << 22))  :
                    color == led_Green   ?  ( 1 << 22 )             :
                    color == led_Blue    ?  ( 1 << 10 )             :
                    color == led_Purple  ?  ((1 << 8) | (1<< 10))   :
                                            0 );
    }else{
        debug_printf ("internal error, impoosible case");
    }
    led_status = (led_status | ~led_bits ) & led_bits;
    debug_printf("setting LED %d to %d\t(0x%x)\n", led, color, led_status);
    //GPIO_WR(1 , (led_status & 0x000000ff) >> 0);    //8D
    //GPIO_WR(2 , (led_status & 0x0000ff00) >> 8);    //4E
    //GPIO_WR(3 , (led_status & 0x00ff0000) >> 16);   //4F
}

/* Update the volume of the audio hardware 
 */
void AudioHwRemote_Volume_Update(chanend c_audiohwremote, unsigned channel, unsigned val)
{
    unsigned AVol, DVol;
    {AVol, DVol} = ADVolume_Split(channel, val);
    unsafe
    {
        c_audiohwremote <: (unsigned) AUDIOHW_CMD_VOLUME_UPDATE;;
        c_audiohwremote <: channel;
        c_audiohwremote <: AVol;
        c_audiohwremote <: DVol;
    }
    unsigned color;
    color = val <=0  ?  led_Red :
            val <=8  ?  led_Orange:
            val <=16 ?  led_Green:
            val <=24 ?  led_Blue:
            val <=32 ?  led_Purple:
                        led_White;
    AudioHwRemote_LED_Update(LED_Volume, color);
}

void AudioHwRemote_Balance_Update(chanend c_audiohwremote, unsigned val)
{
    unsafe
    {
        c_audiohwremote <: (unsigned) AUDIOHW_CMD_BALANCE_UPDATE;;
        c_audiohwremote <: val;
    }
}

static inline void AudioHwRemote_Comp_Update(unsigned item, unsigned channel, int value)
{
    unsafe
    {
        uc_audiohw1 <: (unsigned) AUDIOHW_CMD_COMP_UPDATE;
        uc_audiohw1 <: item;
        uc_audiohw1 <: channel;
        uc_audiohw1 <: value;
    }
}

void process_xscope(chanend xscope_data_in) {
    int bytesRead = 0;
    unsigned char buffer[256];

    xscope_connect_data_from_host(xscope_data_in);

    while (1) {
        select {
            case xscope_data_from_host(xscope_data_in, buffer, bytesRead):
                if (bytesRead) {
                    printstr(buffer);
                    int value = atoi(buffer);
                    debug_printf("param: %d\n", value);
                    int i = 0;
                    //while ((i < bytesRead) && ('0' <= buffer[i]) && (buffer[i] <= '9')){
                    //    i++;
                    //}
                    for (i = 0; (i < bytesRead) && ('0' <= buffer[i]) && (buffer[i] <= '9'); i++) {
                    }
                    if (buffer[i] == 'l')
                    {
                        if      (buffer[i+1] == '2'){
                            AudioHwRemote_Comp_Update (THD_C2,  CH_LEFT, value);
                        }else if(buffer[i+1] == '3'){
                            AudioHwRemote_Comp_Update (THD_C3,  CH_LEFT, value);
                        }else if(buffer[i+1] == 'x'){
                            AudioHwRemote_Comp_Update (XTLK,    CH_LEFT, value);
                        }else{
                            debug_printf("internal error, impossible case");
                            break;
                        }     
                        break;
                    }
                    else if (buffer[i] == 'r')
                    {
                        if      (buffer[i+1] == '2'){
                            AudioHwRemote_Comp_Update (THD_C2,  CH_RIGHT, value);
                        }else if(buffer[i+1] == '3'){
                            AudioHwRemote_Comp_Update (THD_C3,  CH_RIGHT, value);
                        }else if(buffer[i+1] == 'x'){
                            AudioHwRemote_Comp_Update (XTLK,    CH_RIGHT, value);
                        }else{
                            debug_printf("internal error, impossible case");
                            break;
                        }                            
                        break;
                    }
                    else{
                        debug_printf("internal error, impossible case");
                        break;
                    }
                    break;
                }
            break;
        }
    }
}

/* Note this is called from tile[1] but the I2C lines to the CODEC are on tile[0]
 * use a channel to communicate CODEC reg read/writes to a remote core 
 */
void csd_AudioHwInit(const csd_config_t &config)
{
    unsigned regVal = 0;
    uint8_t AnalogVolume = 0; 
    uint8_t UserVolummeL = 0, UserVolummeR = 0;  //0.5dB steps
    int16_t ThdCompC2Ch1 = 0, ThdCompC3Ch1 = 0; 
    int16_t ThdCompC2Ch2 = 0, ThdCompC3Ch2 = 0; 
    int16_t CrosstalkCompCh1 = 0, CrosstalkCompCh2 = 0; //0x0001 is -126dB 
    //uint32_t VolumeMasterTrim = 0x7FFFFFFF;
    
    debug_printf("=====================================================\n");
    debug_printf("Build with XTC Tools %d.%d.%d on %s\n", __XMOS_XTC_VERSION_MAJOR__, __XMOS_XTC_VERSION_MINOR__, __XMOS_XTC_VERSION_PATCH__, __DATE__ );
    debug_printf("=====================================================\n");    
    delay_milliseconds(1);

    // Set the fractional divider if used
    sw_pll_fixed_clock(config.default_mclk);
    debug_printf("sw_pll_fixed_clock_Done\n");
    delay_milliseconds(1);

    /* Take CODEC out of reset */
    GPIO_WR(0 , CODEC_RELEASE_RESET);
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
    debug_printf(ANSI_CYAN "CLOCK CONFIGURATION: \"MCLK => PLL => SYSTEM CLOCK\"" ANSI_RESET "\n");
    delay_milliseconds(1);
#endif
        
    // Check we can talk to the DAC
    DAC_REGREAD(0x40, regVal);
    assert(regVal != 0 && msg(ANSI_RED "DAC Chip ID Register Read Problem" ANSI_RESET "\n"));
    debug_printf(ANSI_CYAN "CHIPI_ID:\t0x%x\tAUTOMUTE:\t0x%x\tDPLL_LOCK:\t0x%x" ANSI_RESET "\n", (regVal >>2), (regVal & 0x03), (regVal & 0x01));

    //for (unsigned reg = 0 ; reg < 61 ; reg++ ){
    //    DAC_REGREAD(reg, regVal);
    //    // debug_printf("I2C_READ\tAddr: 0x%x\t(%d)\tValue: 0x%x\t(%d)\n", reg, reg, regVal, regVal );
    //}
    
    // General Comment:
    // XMOS's I2C lib is very low overhead on physical layer. There is no need to batch send I2C commands. 

    DAC_REGWRITE    (ES9219Q_AMP_CONFIG,                    (uint8_t)(ES9219Q_AMP_PDB_SS | ES9219Q_AMP_MODE) ); 
    DAC_REGWRITE    (ES9219Q_ANALOG_VOL_CTRL,               ((0b010 << 5) | (AnalogVolume & 0x1F)));
    DAC_REGWRITE    (ES9219Q_GPIO12_CONFIG,                 ((3 << 4) | 1) ); //GPIO2 CLK out, GPIO1 lock status
    DAC_REGWRITE    (ES9219Q_VOL_CTRL_LO,                   UserVolummeL ); //GPIO2 CLK out, GPIO1 lock status
    DAC_REGWRITE    (ES9219Q_VOL_CTRL_HI,                   UserVolummeR ); //GPIO2 CLK out, GPIO1 lock status
    DAC_REGWRITE    (ES9219Q_DOP_N_VOL_RAMP_RATE,           (ES9219Q_DOP_N_VOL_RAMP_RATE_VALUE | ES9219Q_DOP_ENABLE | ES9219Q_VOLUME_RATE) );
    DAC_REGWRITE    (ES9219Q_FILTER_SHAPE_N_SYSTEM_MUTE,    (ES9219Q_FILTER_SHAPE | ES9219Q_BYPASS_OSF | ES9219Q_MUTE) );
    //DAC_REGWRITE    (ES9219Q_MASTER_MODE_N_SYNC_CONFIG,     ((0b01 << 5) | (0 << 4) || 2) ); //DATA_CLK = MCLK/4 for 192KHz, Disable MCLK = 128FS, DPLL 5461 FS edge Lock. 
    //DAC_REGWRITE    (ES9219Q_MASTER_TRIM_BYTE0,             (uint8_t)((VolumeMasterTrim >> 0) & 0xFF) ); 
    //DAC_REGWRITE    (ES9219Q_MASTER_TRIM_BYTE1,             (uint8_t)((VolumeMasterTrim >> 1) & 0xFF) ); 
    //DAC_REGWRITE    (ES9219Q_MASTER_TRIM_BYTE2,             (uint8_t)((VolumeMasterTrim >> 2) & 0xFF) ); 
    //DAC_REGWRITE    (ES9219Q_MASTER_TRIM_BYTE3,             (uint8_t)((VolumeMasterTrim >> 3) & 0xFF) );    
    //DAC_REGWRITE    (ES9219Q_GENERAL_CONFIG,              TBD ); 
    //DAC_REGWRITE    (ES9219Q_GPIO_CONFIG_N_AUTO_CLK_GEAR, TBD ); 

    DAC_REGWRITE    (ES9219Q_CHARGE_PUMP_CLOCK_CONFIG_LO,   (uint8_t)(ES9219Q_CP_CLK_DIV & 0xFF)); 
    DAC_REGWRITE    (ES9219Q_CHARGE_PUMP_CLOCK_CONFIG_HI,   (uint8_t)(ES9219Q_CP_CLK_SEL | ES9219Q_CP_CLK_EN | ((ES9219Q_CP_CLK_DIV >> 8) & 0xFF)) ); 
    
    DAC_REGWRITE    (ES9219Q_THD_BYPASS_N_MONO_MODE,        ( ES9219Q_BYPASS_THD | ES9219Q_MONO_MODE )); // Enable THD Compensation
    DAC_REGWRITE    (ES9219Q_CROSSTALK_COMP_CONFIG,         (uint8_t)(ES9219Q_BYPASS_CT | ES9219Q_ENABLE_PLL_LOCK ) ); 
    AudioHwRemote_Comp_Update (THD_C2,  CH_LEFT,    ThdCompC2Ch1       );
    AudioHwRemote_Comp_Update (THD_C2,  CH_RIGHT,   ThdCompC2Ch2       );
    AudioHwRemote_Comp_Update (THD_C3,  CH_LEFT,    ThdCompC3Ch1       );
    AudioHwRemote_Comp_Update (THD_C3,  CH_RIGHT,   ThdCompC3Ch2       );
    AudioHwRemote_Comp_Update (XTLK,    CH_LEFT,    CrosstalkCompCh1   );
    AudioHwRemote_Comp_Update (XTLK,    CH_RIGHT,   CrosstalkCompCh2   );
    
    //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_N_I2S_MON_CONFIG,  (uint8_t)(ES9219Q_DISALBE_ATR_CH2 | ES9219Q_DISALBE_ATR_CH1 | ES9219Q_PDB_ATR_R | ES9219Q_PDB_ATR_L) );     
    //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_OVERRIDE_1,        (uint8_t)(ES9219Q_CPH_APDB) );     
    //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_OVERRIDE_1,        (uint8_t)(ES9219Q_CPH_APDB | ES9219Q_ENAUX | ES9219Q_AREG_PDB | ES9219Q_ENPHA | ES9219Q_CPH_ENS | ES9219Q_CPH_ENW | ES9219Q_CP_CLKIO_SEL) );     
    //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_OVERRIDE_2,        (uint8_t)(ES9219Q_DIG_OVER_EN | ES9219Q_SEL1V | ES9219Q_SHTOUTB | ES9219Q_SHTINB) );     
    //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_OVERRIDE_3,        (uint8_t)(ES9219Q_ENFCB | ES9219Q_ENCP_OE | ES9219Q_ENAUX_OE | ES9219Q_CPL_ENS | ES9219Q_CPL_ENW | ES9219Q_SEL3V3_PS | ES9219Q_ENSM_PS | ES9219Q_SEL3V3_CPH ) );     
    //DAC_REGWRITE    (ES9219Q_ANALOG_CTRL_SIGNALS,           (uint8_t)((1 << 6) | (1 << 3) | (1 << 2) | 1) ); 
    // delay_milliseconds(10);

    // uint32_t fir1[128] = {};
    // for (unsigned i = 0 ; i < 128 ; i++ ){
    //     DAC_REGWRITE    (ES9219Q_FIR_RAM_ADDR,         (uint8_t)( TBD ) ); 
    //     DAC_REGWRITE    (ES9219Q_FIR_RAM_DATA_BYTE0,   (uint8_t)((fir1[i] >> 0) & 0xFF ) ); 
    //     DAC_REGWRITE    (ES9219Q_FIR_RAM_DATA_BYTE1,   (uint8_t)((fir1[i] >> 8) & 0xFF ) ); 
    //     DAC_REGWRITE    (ES9219Q_FIR_RAM_DATA_BYTE2,   (uint8_t)((fir1[i] >> 16) & 0xFF ) ); 
    //     DAC_REGWRITE    (ES9219Q_FIR_CONFIG,           (uint8_t)( 0x02 ) ); 
    // }   
    // DAC_REGWRITE    (ES9219Q_FIR_CONFIG,           (uint8_t)(SetEvenBits ? 0x04 : 0x00) ); 


    // uint32_t fir2[16] = {};
    // for (unsigned i = 0 ; i < 16 ; i++ ){
    //     DAC_REGWRITE    (ES9219Q_FIR_RAM_ADDR,         (uint8_t)( TBD ) ); 
    //     DAC_REGWRITE    (ES9219Q_FIR_RAM_DATA_BYTE0,   (uint8_t)((fir2[i] >> 0) & 0xFF ) ); 
    //     DAC_REGWRITE    (ES9219Q_FIR_RAM_DATA_BYTE1,   (uint8_t)((fir2[i] >> 8) & 0xFF ) ); 
    //     DAC_REGWRITE    (ES9219Q_FIR_RAM_DATA_BYTE2,   (uint8_t)((fir2[i] >> 16) & 0xFF ) ); 
    //     DAC_REGWRITE    (ES9219Q_FIR_CONFIG,           (uint8_t)( 0x02 ) ); 
    // }   
    // DAC_REGWRITE    (ES9219Q_FIR_CONFIG,           (uint8_t)(SetEvenBits ? 0x04 : 0x00) ); 


    //for (unsigned reg = 0 ; reg < 61 ; reg++ ){
    //    DAC_REGREAD(reg, regVal);
    //     debug_printf("I2C_READ\tAddr: 0x%x\t(%d)\tValue: 0x%x\t(%d)\n", reg, reg, regVal, regVal );
    //}
}

/* Configures the external audio hardware for the required sample frequency.
 * See gpio.h for I2C helper functions and gpio access
 */
void csd_AudioHwConfig( unsigned samFreq, unsigned mClk, unsigned dsdMode,
                        unsigned sampRes_DAC, unsigned sampRes_ADC)
{    
    assert(samFreq >= 22050);
    sw_pll_fixed_clock(mClk);

    //also try query i2c bus to get DoP mode
    // show DSD LED is native DSD is detected 
    AudioHwRemote_LED_Update  (LED_DSD_Mode,  ( samFreq > 768000 ?  led_Purple : led_Green   ));      
    AudioHwRemote_LED_Update  (LED_samFreq,   ( samFreq <= 48000 ?  led_Red :
                                                samFreq <= 96000 ?  led_Orange :
                                                samFreq <= 19200 ?  led_Green :
                                                samFreq <= 38400 ?  led_Blue :
                                                                    led_Purple )
            );
    // delay_milliseconds(10);
    {
        unsigned regVal = 0;
        debug_printf("===================Current State===============\n");   
        DAC_REGREAD(ES9219Q_GPIO_READBACK, regVal);
        debug_printf(ANSI_GREEN "CLK_GEAR: %d\tGPIO2: %d\tGPIO1: %d" ANSI_RESET "\n", (regVal>>2)&3, (regVal>>1)&1, (regVal>>0)&1);   
        DAC_REGREAD(ES9219Q_READ_INPUT_SEL_N_AUTOMUTE_STAT, regVal);        
        debug_printf(ANSI_GREEN "OC_R: %d\tOC_L: %d\tAUTOMUTE_R: %d\tAUTOMUTE_L: %d" ANSI_RESET "\n", (regVal>>7)&1, (regVal>>6)&1, (regVal>>5)&1, (regVal>>4)&1);   
        DAC_REGREAD(ES9219Q_READ_LOCK_STATUS, regVal);
        debug_printf(ANSI_GREEN "MQA_LOCK: %d\tPLL_LOCK: %d\tASRC: %d" ANSI_RESET "\n", (regVal>>2)&1, (regVal>>1)&1, (regVal)&1);   
        DAC_REGREAD(ES9219Q_CHIP_STATUS, regVal);
        debug_printf(ANSI_GREEN "CHIPI_ID: %d\tAUTOMUTE: %d\tDPLL_LOCK: %d" ANSI_RESET "\n", (regVal >>2), (regVal & 0x03), (regVal & 0x01));   
     }

}


void csd_AudioHwConfig_Mute(void){
    DAC_REGWRITE    (ES9219Q_MUTE, 1 ); 
}


void csd_AudioHwConfig_UnMute(void){
    DAC_REGWRITE    (ES9219Q_MUTE, 0 ); 
}