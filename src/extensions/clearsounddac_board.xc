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
#include "xua_defs.h"
#if PCM_DSP_EN
#include "csd_dsp.h"
#endif

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

/* Legacy colour/LED selector constants (LED_DSD_Mode, LED_samFreq, LED_Volume,
 * led_Red..led_White) removed with the dead AudioHwRemote_LED_Update ladder —
 * the display is the tile[0] state machine (LED_ST_* + rate tier). */

// DAC I2C lines
on tile[0]: port p_i2c_scl = PORT_I2C_SCL;
on tile[0]: port p_i2c_sda = PORT_I2C_SDA;

on tile[0]: in buffered port:1 p_butt_up    = PORT_BUTTON_UP; 
on tile[0]: in buffered port:1 p_butt_down  = PORT_BUTTON_DOWN; 

// DAC AMP reset line
on tile[0]: out port p_codec_reset  = PORT_CODEC_RST_N;
on tile[0]: out port p_led_024      = XS1_PORT_4F;
on tile[0]: out port p_led_35       = XS1_PORT_4E;
on tile[0]: out port p_led_d3g      = PORT_LED_D3G;
// on tile[0]: out port p_led_1        = XS1_PORT_8D;


// CODEC Reset bit mask
#define CODEC_RELEASE_RESET      (0x30) // Release codec from reset

/* ---- LED status display + mute indicator ------------------------------
 * bring-over fd9242e (board-proven), adapted: mute-blue joins the same
 * state machine so display arbitration lives in exactly one place.
 * RGB dies common-anode to 3.3VD -> driven-LOW = lit.
 * Datasheet pin table (XU316-1024-QF60A):
 *   4F: b1=X0D29 D1-R   b2=X0D30 D1-B   b3=X0D31 D2-G
 *   4E: b2=X0D32 D2-B   b3=X0D33 D2-R          (b0/b1 not bonded)
 *   8D: b4=X0D40 DAC_MUTE_N  b5=X0D41 AMP_EN  b6=X0D42 D1-G  b7=X0D43 D3-R
 *   1A: X0D00 D3-G (PORT_LED_D3G)
 * D3-B unwired (R36.1 floats). CODEC_RELEASE_RESET 0x30 keeps mute_n/amp_en
 * high. Display priority: mute-blue > chipid-fail > bypass-3s > rate tier > off.
 * Sole writer of these ports and of reg 7: AudioHwRemoteTile0. */
#define LED_ST_SOLID_RED     0   /* power-on default: all three red solid */
#define LED_ST_CHIPID_FAIL   1   /* chip-ID unreadable: blink red forever */
#define LED_ST_BYPASS        2   /* PLL bypassed: blink red 3 s, then normal */
#define LED_ST_ALL_OFF       3   /* DAC healthy, no rate from host yet */
#define LED_ST_RATE          4   /* rate tier display */
#define LED_ST_MUTED         5   /* host mute: only blue dies lit */

#define LED_8D_OFF_BITS (0xC0)    /* D1-G, D3-R high = off */

/* Host mute intent + display state. Written only by AudioHwRemoteTile0. */
static unsigned host_muted = 0;
static unsigned led_state  = LED_ST_SOLID_RED;
static unsigned led_on     = 1;      /* blink phase */
static unsigned led_tier   = 0;      /* last rate tier */
/* Last 8D reset word requested by GPIO_WR bank 0; the renderer ORs LED bits
 * on top of it so both intents compose inside the one writer task. */
static unsigned gpio_8d_reset = CODEC_RELEASE_RESET;

/* One writer for all four LED ports: render the current display state. */
static void led_write_words(unsigned v4f, unsigned v4e, unsigned v8d, unsigned d3g)
{
    p_led_024    <: v4f;
    p_led_35     <: v4e;
    p_codec_reset <: (gpio_8d_reset & 0x3F) | (v8d & 0xC0);  /* reset bits from GPIO_WR, LED bits here */
    p_led_d3g    <: d3g;
}

static void led_render(void)
{
    switch (led_state) {
        case LED_ST_MUTED:       /* every lit die is blue */
            led_write_words(0x0B, 0x0B, LED_8D_OFF_BITS, 1);
            break;
        case LED_ST_CHIPID_FAIL:
        case LED_ST_BYPASS:
            if (led_on) led_write_words(0xD, 0x7, 0x70, 1);   /* D1-R, D2-R, D3-R */
            else        led_write_words(0xF, 0xF, LED_8D_OFF_BITS, 1);
            break;
        case LED_ST_ALL_OFF:
            led_write_words(0xF, 0xF, LED_8D_OFF_BITS, 1);
            break;
        case LED_ST_RATE:
            /* Words verified against the XU316-1024-QF60A datasheet pad table +
             * current ASB netlist (LOW = lit): 4F{b1 D1-R, b2 D1-B, b3 D2-G},
             * 4E{b2 D2-B, b3 D2-R}, 8D{b6 D1-G, b7 D3-R}, 1A{D3-G}. Tiers accumulate. */
            switch (led_tier) {
                case 1:  led_write_words(0xF, 0xF, 0xB0, 1); break;   /* D1-G */
                case 2:  led_write_words(0x7, 0xF, 0xB0, 1); break;   /* +D2-G */
                case 3:  led_write_words(0x7, 0xF, 0x00, 0); break;   /* +D3 yellow (D3-R b7 low, D3-G low) */
                default: led_write_words(0x7, 0xF, 0xB0, 0); break;   /* tier4/DSD: all green */
            }
            break;
        default:                 /* SOLID_RED */
            led_write_words(0xD, 0x7, 0x70, 1);
            break;
    }
}

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

#define VOL_INTERNAL_MIN_DB8 (-64 * 256)
#define VOL_BTN_STEP_DB8     (256)

/* ---- Volume mixer (bring-over a5bc0ed, adapted). SOLE OWNER:
 * AudioHwRemoteTile0 — declared here only because XC takes file-scope
 * statics, not task-local functions. The host terms come from ep0's
 * AudioHwRemote_Volume_Update split words; the button term is a shared
 * offset, 0 dB at power-up, +/-1 dB (256) per press.
 * BUT_0 = X0D36/1M = UP (S2), BUT_1 = X0D37/1N = DOWN (S1); pad -> switch
 * -> R19/R20 -> 1.8VD so pressed = high, released floats -> pull-downs.
 * The SUM is kept inside internal limits 0..-64 dB (wider than the
 * USB-advertised -48..0) by coercing the BUTTON term only, never the host
 * term. Writes deduped at the register boundary (072111a). */
static int      volHostDl8[2] = {0, 0};       /* digital term per channel, 8.8 dB */
static int      volBtnDb8     = 0;            /* button offset, 8.8 dB */
static unsigned volRegLastD[2]  = {0, 0};     /* dedupe for regs 15/16 */
static unsigned volRegValidD[2] = {0, 0};
static unsigned volRegLastA  = 0;             /* dedupe for reg 3 (analog) */
static unsigned volRegValidA = 0;

static void vol_clamp_button(void)
{
    int hmax = (volHostDl8[0] > volHostDl8[1]) ? volHostDl8[0] : volHostDl8[1];
    int hmin = (volHostDl8[0] < volHostDl8[1]) ? volHostDl8[0] : volHostDl8[1];
    if (volBtnDb8 + hmax > 0) {
        volBtnDb8 = -hmax;                        /* total pinned at    0 dB */
    }
    if (volBtnDb8 + hmin < VOL_INTERNAL_MIN_DB8) {
        volBtnDb8 = VOL_INTERNAL_MIN_DB8 - hmin;  /* total pinned at  -64 dB */
    }
}

/* Register 3 is ANALOG_VOLUME_MAIN: per the ES9219 datasheet it "controls both
 * channel 1 and channel 2 analog volume controls". It is ONE register for the
 * pair, so it can only ever carry an attenuation both channels agree on. The
 * split used to derive it per channel, so on an asymmetric request the louder
 * channel wrote 0 dB over the quieter channel's -24 dB and that channel kept
 * only its digital trim -- it stopped at -8 dB when the host asked for -32.
 * Measured on the bench: 64,192 -> -8.0 dB while 192,64 -> -34.3 dB for the
 * same nominal request (rig/vol_analog.py).
 * So take the analog term from the LOUDEST channel: analog has priority for
 * noise performance, and the loudest request is the largest attenuation that
 * is still common to both. Whatever each channel still needs beyond it is
 * made up in the per-channel digital volume (regs 15/16), which is genuinely
 * independent and covers 0..-127.5 dB at 0.5 dB/LSB. */
static int vol_analog_common(void)
{
    int const A_RES = 512, A_RANGE = (-24 * 256);   /* 2 dB step, -24 dB floor */
    int t0 = volHostDl8[0] + volBtnDb8;
    int t1 = volHostDl8[1] + volBtnDb8;
    int tmax = (t0 > t1) ? t0 : t1;
    if (tmax > 0) tmax = 0;                 /* never amplify */
    if (tmax < A_RANGE) tmax = A_RANGE;     /* analog cannot go below -24 dB */
    /* Truncation toward zero on a negative dividend is a ceil, which is what
     * the original split used to round an attenuation toward 0 dB. */
    return (tmax / A_RES) * A_RES;
}

static void vol_apply_analog(int aCom, client interface i2c_master_if i2c)
{
    unsigned regValueA = (unsigned)((-aCom) >> 8) & 0x1F;
    if (volRegValidA && volRegLastA == regValueA) return;
    volRegLastA = regValueA; volRegValidA = 1;
    ES9219Q_REGWRITE(ES9219Q_ANALOG_VOL_CTRL, ((0b010 << 5) | regValueA), i2c);
}

static void vol_apply_digital(int ch, int aCom, client interface i2c_master_if i2c)
{
    /* aCom is applied to both channels by the analog stage, so the digital
     * trim carries only what is left for THIS channel. */
    int total = volHostDl8[ch] + volBtnDb8 - aCom;
    unsigned reg = (ch == 0) ? ES9219Q_VOL_CTRL_LO : ES9219Q_VOL_CTRL_HI;
    int code = (total <= 0) ? (-total + 64) / 128 : 0;   /* 0.5 dB/LSB, round */
    if (code > 255) code = 255;
    if (volRegValidD[ch] && volRegLastD[ch] == (unsigned)code) return;   /* dedup */
    volRegLastD[ch]  = (unsigned)code;
    volRegValidD[ch] = 1;
    ES9219Q_REGWRITE(reg, (unsigned)code, i2c);
    debug_printf("VOLWR ch:%d code:%d tot88:%d host88:%d btn88:%d aCom88:%d\n",
                 ch, code, total, volHostDl8[ch], volBtnDb8, aCom);
}

/* One entry point: the analog term depends on BOTH channels, so any change to
 * either channel's host term or to the shared button term must recompute all
 * three registers together. Calling vol_apply_digital for one channel alone
 * would leave reg 3 describing a level neither channel asked for. */
static void vol_apply_all(client interface i2c_master_if i2c)
{
    int aCom = vol_analog_common();
    vol_apply_analog(aCom, i2c);
    vol_apply_digital(0, aCom, i2c);
    vol_apply_digital(1, aCom, i2c);
}

[[combinable]] void AudioHwRemoteTile0(chanend c[n], unsigned n, client interface i2c_master_if i2c)
{
    /* ---- Volume mixer (bring-over a5bc0ed, adapted): this task is the SOLE
     * writer of codec volume regs 3/15/16 after boot and the sole owner of
     * everything below.
     *  host term   : last split words from ep0's AudioHwRemote_Volume_Update
     *                (valueA / valueDL, signed 8.8 dB), per channel slot.
     *  button term : shared offset, 0 dB at power-up, +/-1 dB (256) per press.
     *                BUT_0 = X0D36/1M = UP (S2), BUT_1 = X0D37/1N = DOWN (S1);
     *                pad -> switch -> R19/R20 -> 1.8VD so pressed = high and
     *                released floats -> internal pull-downs.
     *  The SUM is kept inside the internal limits 0..-64 dB (wider than the
     *  USB-advertised -48..0) by coercing the BUTTON term only, never the
     *  host term. Writes are deduped at the register boundary (072111a). */
    unsigned btn_up_val = 0, btn_dn_val = 0;
    unsigned btn_up_stable = 1, btn_dn_stable = 1;
    timer btn_up_tmr, btn_dn_tmr;
    unsigned btn_up_to = 0, btn_dn_to = 0;
    const unsigned BTN_DEBOUNCE_TICKS = 5000000u;   /* 50 ms @ 100 MHz */
    set_port_pull_down(p_butt_up);
    set_port_pull_down(p_butt_down);

    /* LED blink scheduler: 0.5 s phase grid (1 Hz blink) for the error states. */
    timer led_tmr;
    unsigned led_now;
    unsigned led_bypass_until = 0;
    led_tmr :> led_now;
    led_render();                                 /* power-on: solid red */

    while(1)
    {
        select{
            case led_tmr when timerafter(led_now) :> void:
            {
                if (led_state == LED_ST_CHIPID_FAIL || led_state == LED_ST_BYPASS) {
                    led_on = !led_on;
                    if (led_state == LED_ST_BYPASS && led_on &&
                        (int)(led_now - led_bypass_until) >= 0) {
                        /* 3 s window elapsed: drop bypass indication */
                        led_state = (led_tier != 0) ? LED_ST_RATE : LED_ST_ALL_OFF;
                    }
                }
                led_now += 50000000;              /* 0.5 s @ 100 MHz */
                led_render();
            }
            break;

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
                    /* bring-over fd9242e: GPIO_WR bank 0 updates the reset word,
                     * bank 2/3 (4E/4F LED requests) refresh the display; all port
                     * writes go through led_write_words so reset + LED bits
                     * compose without ever fighting for the same pins. */
                    unsigned bank, value;
                    c[i] :> bank;
                    c[i] :> value;

                    if (0 == bank) {
                        gpio_8d_reset = value & 0x30;
                        led_render();
                    } else if (2 == bank || 3 == bank) {
                        led_render();             /* display owns 4E/4F now */
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
                    /* Host SET_CUR split words from ep0 (bring-over 072111a
                     * merged with a5bc0ed mixer): update the host terms, then
                     * rewrite regs 3/15/16 from (host + button). Value repeats
                     * are dropped at the register boundary. */
                    unsigned channel;
                    int valueA, valueDL, valueDR;
                    c[i] :> channel;
                    c[i] :> valueA;
                    c[i] :> valueDL;
                    valueDR = valueDL;

                    if (channel < 2) {
                        /* Keep the FULL host request per channel (analog +
                         * digital halves of ep0's split). The mixer must be
                         * able to re-partition the total itself, because the
                         * analog half is one shared register: partitioning at
                         * ep0 per channel is what lost the quieter channel's
                         * -24 dB when the louder channel rewrote reg 3. */
                        volHostDl8[channel] = valueA + valueDL;
                        vol_clamp_button();
                        /* Analog term is COMMON to both channels (reg 3 is one
                         * register), so it cannot be recomputed from this
                         * channel's word alone. Re-derive all three registers
                         * from both host terms. */
                        vol_apply_all(i2c);
                    }
                    debug_printf("VOLCMD ch:%d A:%d DL:%d\n", channel, valueA, valueDL);
                }
                else if (cmd == AUDIOHW_CMD_MUTE_SET)
                {
                    /* Host mute intent from AudioHwRemote_Volume_Update. Reg 7 bit0
                     * is the codec system mute, so the digital/analog volume regs are
                     * left untouched and unmute restores the previous level exactly.
                     * Display: mute-blue outranks everything while held. */
                    unsigned muted;
                    c[i] :> muted;
                    if (muted != host_muted)
                    {
                        host_muted = muted;
                        ES9219Q_REGWRITE(ES9219Q_FILTER_SHAPE_N_SYSTEM_MUTE,
                            (ES9219Q_FILTER_SHAPE | ES9219Q_BYPASS_OSF |
                             (muted ? ES9219Q_MUTE : ES9219Q_UNMUTE)), i2c);
                        led_state = muted ? LED_ST_MUTED
                                          : ((led_tier != 0) ? LED_ST_RATE : LED_ST_ALL_OFF);
                        led_render();
                        debug_printf(ANSI_YELLOW "MUTE_SET: %d" ANSI_RESET "\n", muted);
                    }
                }
                else if (cmd == AUDIOHW_CMD_LED_STATUS)
                {
                    /* bring-over fd9242e: boot health report from AudioHwInit */
                    unsigned st;
                    c[i] :> st;
                    debug_printf("LED_STATUS: %d\n", st);
                    if (st == LED_ST_CHIPID_FAIL) { led_state = LED_ST_CHIPID_FAIL; led_on = 1; }
                    else if (st == LED_ST_BYPASS) {
                        led_state = LED_ST_BYPASS; led_on = 1;
                        led_bypass_until = led_now + 300000000u;    /* 3 s */
                    }
                    else if (st == LED_ST_ALL_OFF && !host_muted) {
                        led_state = (led_tier != 0) ? LED_ST_RATE : LED_ST_ALL_OFF;
                    }
                    led_render();
                }
                else if (cmd == AUDIOHW_CMD_LED_RATE)
                {
                    /* bring-over fd9242e: rate tier from AudioHwConfig */
                    unsigned f, dsd;
                    c[i] :> f;
                    c[i] :> dsd;
                    if (dsd != 0)              led_tier = 4;
                    else if (f <= 48000)       led_tier = 1;
                    else if (f <= 96000)       led_tier = 2;
                    else if (f <= 192000)      led_tier = 3;
                    else                       led_tier = 4;
                    if (!host_muted && led_state != LED_ST_CHIPID_FAIL && led_state != LED_ST_BYPASS) {
                        led_state = LED_ST_RATE;
                        led_render();
                    }
                }
                else if (cmd == AUDIOHW_CMD_MUTE_RESYNC)
                {
                    /* AudioHwConfig's unmute just overwrote reg 7; re-assert mute. */
                    ES9219Q_REGWRITE(ES9219Q_FILTER_SHAPE_N_SYSTEM_MUTE,
                        (ES9219Q_FILTER_SHAPE | ES9219Q_BYPASS_OSF |
                         (host_muted ? ES9219Q_MUTE : ES9219Q_UNMUTE)), i2c);
                }
                else if (cmd == AUDIOHW_CMD_EXIT)
                {
                    i2c.shutdown();
                    return;
                }
                break;

            /* ---- Button volume (bring-over a5bc0ed): 50 ms debounce FSM
             * inlined in the mixer's own task — XC forbids exchanging
             * channels between tasks combined onto one thread, and only this
             * task may mutate the mixer. Active-high pads (pull-downs set at
             * task entry); act on the edge confirmed stable for the window. */
            case btn_up_stable => p_butt_up when pinsneq(btn_up_val) :> btn_up_val:
                btn_up_stable = 0;
                btn_up_tmr :> btn_up_to;
                btn_up_to += BTN_DEBOUNCE_TICKS;
                break;
            case !btn_up_stable => btn_up_tmr when timerafter(btn_up_to) :> void:
                btn_up_stable = 1;
                if (btn_up_val == 1) {                  /* confirmed press */
                    volBtnDb8 += VOL_BTN_STEP_DB8;      /* +1 dB */
                    vol_clamp_button();
                    debug_printf("BTN_UP btn88:%d\n", volBtnDb8);
                    vol_apply_all(i2c);
                }
                break;

            case btn_dn_stable => p_butt_down when pinsneq(btn_dn_val) :> btn_dn_val:
                btn_dn_stable = 0;
                btn_dn_tmr :> btn_dn_to;
                btn_dn_to += BTN_DEBOUNCE_TICKS;
                break;
            case !btn_dn_stable => btn_dn_tmr when timerafter(btn_dn_to) :> void:
                btn_dn_stable = 1;
                if (btn_dn_val == 1) {                  /* confirmed press */
                    volBtnDb8 -= VOL_BTN_STEP_DB8;      /* -1 dB */
                    vol_clamp_button();
                    debug_printf("BTN_DOWN btn88:%d\n", volBtnDb8);
                    vol_apply_all(i2c);
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
            /* Buttons are serviced inside AudioHwRemoteTile0 (50 ms debounce
             * inlined there — the mixer state must live with the port owner).
             * button_debounce_task instances removed: a port may only be
             * selected by one task. */
                        /* 3 client channels now: 0/1 as before, plus index 3 for the
             * diagnostic core's codec-register access. AudioHwRemoteTile0
             * already dispatches AUDIOHW_CMD_REGRD/REGWR generically for any
             * c[i] it selects on, so this adds no server code and, more
             * importantly, no second I2C client -- the task at c[3] shares the
             * one bus owner instead of racing it. */
            AudioHwRemoteTile0(c, 4, i2c[0]);
        }
    }
}

unsafe chanend uc_audiohw1; // audio core to i2c 
// unsafe chanend uc_audiohw2;  //i2c hardware volume control from lib_xua
unsafe chanend uc_audiohw3; //xscope in
unsafe chanend uc_audiohw4; //button up
unsafe chanend uc_audiohw5; //botton down
/* Diagnostic core's codec-register channel (SCPI over USB MIDI). Bound to
 * c_audiohw[3] in src/core/user_main.h; the tile-0 server services it through
 * the same `case c[int i]` branch as the others, so no server-side code is
 * needed for REGWR/REGRD. Dedicated index on purpose: sharing uc_audiohw1
 * would let a bench request interleave with the audio core's own request/reply
 * and read back the wrong answer. */
unsafe chanend uc_audiohw_diag;

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

/* Mute intent to the server task (sole writer of reg 7 and the LED ports).
 * Sent on the caller's channel so it stays FIFO-ordered with the volume
 * writes that precede/follow it on the same channel. */
static inline void DAC_MUTE_SET(chanend c, unsigned muted)
{
    unsafe
    {
        c <: (unsigned) AUDIOHW_CMD_MUTE_SET;
        c <: muted;
    }
}

/* Re-assert reg 7 for the current host mute state. Called after AudioHwConfig,
 * whose UnMute write would otherwise silently override a host mute. */
static inline void DAC_MUTE_RESYNC(void)
{
    unsafe
    {
        uc_audiohw1 <: (unsigned) AUDIOHW_CMD_MUTE_RESYNC;
    }
}

/* LED status senders (bring-over fd9242e, adapted): the display state machine
 * lives entirely in the tile[0] server task; tile[1] only reports events. */
static inline void DAC_LED_STATUS(unsigned st)
{
    unsafe
    {
        uc_audiohw1 <: (unsigned) AUDIOHW_CMD_LED_STATUS;
        uc_audiohw1 <: st;
    }
}

static inline void DAC_LED_RATE(unsigned samFreq, unsigned dsdMode)
{
    unsafe
    {
        uc_audiohw1 <: (unsigned) AUDIOHW_CMD_LED_RATE;
        uc_audiohw1 <: samFreq;
        uc_audiohw1 <: dsdMode;
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
    printf("Ch:%d\tVol_dB:\t%2.2f\tA:\t%2.2f\tD:\t%2.2f\n", channel, (float)Vol_dB/256, (float)AVol/256, (float)DVol/256);
    return {(int)AVol, (int)DVol};
}

/* Update the volume of the audio hardware 
 */
void AudioHwRemote_Volume_Update(chanend c_audiohwremote, unsigned channel, unsigned val)
{
    unsigned AVol, DVol;
    unsigned muted;
    debug_printf("Volume_Update:\tMIN: %d\tValue: %d\n", (int32_t)(int16_t)MIN_VOLUME, val);
    /* lib_xua forwards UAC2 -inf (0x8000) here on FU_MUTE SET_CUR (updateVol),
     * and the real dB on unmute/volume SET_CUR. Sign-extend, then anything at or
     * below the USB floor is a mute request. The server task is the single owner
     * of mute state, reg 7 and the LED ports, so forward intent unconditionally:
     * reg 7 is a hardware mute and the volume registers are never touched while
     * muted, so the unmute that follows restores the level with nothing cached. */
    muted = ((int)(int16_t)val <= (int)(int16_t)MIN_VOLUME) ? 1u : 0u;
    DAC_MUTE_SET(c_audiohwremote, muted);
    if (muted) return;               /* keep codec regs at last host level */
    {AVol, DVol} = ADVolume_Split(channel, val);
    unsafe
    {
        c_audiohwremote <: (unsigned) AUDIOHW_CMD_VOLUME_UPDATE;;
        c_audiohwremote <: channel;
        c_audiohwremote <: AVol;
        c_audiohwremote <: DVol;
    }
    /* Volume colour ladder removed (bring-over fd9242e): its AudioHwRemote_LED_Update
     * wrote nothing (GPIO_WR lines were already commented out). Display is the
     * tile[0] state machine — rate tier + mute blue. Volume display can be
     * re-added there in the future LED-assignment pass. */
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
    sw_pll_fixed_clock(config.default_mclk);   /* lib_sw_pll pinned v2.4.1: single-arg API (v2.5.0 added required tile_mask; lib_xua develop still calls the old API) */
    debug_printf("sw_pll_fixed_clock_Done\n");
    delay_milliseconds(1);

    /* Take CODEC out of reset */
    GPIO_WR(0 , CODEC_RELEASE_RESET);
    delay_milliseconds(1);
#ifdef ES9219_USE_PLL_WITH_MCLK
    /* Marginal-PLL boards: start with the PLL enabled; if the chip-ID readback
     * fails (core has no clock), switch to PLL bypass and continue.
     * (bring-over ec898a2, validated: bypass locks instantly, all rates play) */
    unsigned idtries = 0;
    unsigned pll_bypass = 0;
    for (;;) {
        // start up sequence per ESS application note
        DAC_PLL_REGWRITE    (201 ,   0x19); //Set DAC Clock input to MCLK
        DAC_PLL_REGWRITE    (193 ,   0xC0); //Turn On PLL Charge Pump and VCo
        DAC_PLL_REGWRITE    (194 ,   0x00); //Set Clock_IN_DIV = 8 and FB_DIV = 131072
        DAC_PLL_REGWRITE    (195 ,   0x00); 
        DAC_PLL_REGWRITE    (196 ,   0x82); 
        if (pll_bypass) {
            DAC_PLL_REGWRITE(197, 0x80);    // PLL_BYPASS=1: clock around the loop
            debug_printf(ANSI_CYAN "CLOCK CONFIGURATION: \"MCLK => BYPASS => SYSTEM CLOCK\"" ANSI_RESET "\n");
        } else {
            DAC_PLL_REGWRITE(197, 0x00);
            debug_printf(ANSI_CYAN "CLOCK CONFIGURATION: \"MCLK => PLL => SYSTEM CLOCK\"" ANSI_RESET "\n");
        }
        DAC_PLL_REGWRITE    (198 ,   0x02); //Set Clock_OUT_Div = 2, and PFE_DELAY to 1.5ns
        DAC_PLL_REGWRITE    (199 ,   0xC2); 
        DAC_PLL_REGWRITE    (200 ,   0x0C); //Turn On PLL regulators as final step
        delay_milliseconds(1);

        // Check we can talk to the DAC (retry: marginal boards boot slow)
        for (idtries = 0; idtries < 20; idtries++) {
            DAC_REGREAD(0x40, regVal);
            if (regVal != 0) break;
            delay_milliseconds(5);
        }
        debug_printf("CHIPID tries: %d (pll_bypass=%d)\n", idtries, pll_bypass);
        if (regVal != 0) break;
        if (pll_bypass) break;          // both paths failed: fall through to assert
        pll_bypass = 1;                 // PLL enabled didn't clock the core: fall back
    }
    if (regVal == 0) {
        DAC_LED_STATUS(LED_ST_CHIPID_FAIL);   /* blink red forever until assert kills us */
        delay_milliseconds(2000);             /* let the tile[0] task render it */
    }
#endif
        
    assert(regVal != 0 && msg(ANSI_RED "DAC Chip ID Register Read Problem" ANSI_RESET "\n"));
    /* DAC functional: bypassed boards blink red 3 s then forget it; healthy
     * boards go dark until the host starts changing rates (bring-over fd9242e). */
    if (pll_bypass) DAC_LED_STATUS(LED_ST_BYPASS);
    else            DAC_LED_STATUS(LED_ST_ALL_OFF);
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
    sw_pll_fixed_clock(mClk);   /* lib_sw_pll pinned v2.4.1 (see note in AudioHwInit) */

#if PCM_DSP_EN
    /* DSP state re-seeds with the new rate family + stream DSD mode.
     * Called from the audiohub core -- same single writer as the frame hook. */
    csd_dsp_set_rate(samFreq);
    csd_dsp_set_dsd(dsdMode);
    csd_dsp_preset_boot();     /* no-op unless -DCSD_DSP_PRESET_DEFAULT=n */
#endif

    /* Display: report the rate to the tile[0] LED state machine (bring-over
     * fd9242e). Replaces the dead AudioHwRemote_LED_Update colour ladder, whose
     * GPIO_WR output was commented out — one display arbiter, one writer. */
    DAC_LED_RATE(samFreq, dsdMode);
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
    DAC_REGWRITE    (ES9219Q_FILTER_SHAPE_N_SYSTEM_MUTE,    (ES9219Q_FILTER_SHAPE | ES9219Q_BYPASS_OSF | ES9219Q_MUTE) );
}


void csd_AudioHwConfig_UnMute(void){
    /* Runs after AudioHwConfig on every rate change. Must not override a host
     * mute: ask the server to re-assert reg 7 for its cached mute state
     * (unmuted -> writes unmuted, exactly as before). */
    DAC_MUTE_RESYNC();
}