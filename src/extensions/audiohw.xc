#include <xs1.h>
#include <assert.h>
#include <platform.h>
#include "xassert.h"
#include "i2c.h"
#include "xua.h"

extern "C" {
    #include "sw_pll.h"
}

#if (XUA_PCM_FORMAT == XUA_PCM_FORMAT_TDM) && (XUA_I2S_N_BITS != 32)
#warning ADC only supports TDM operation at 32 bits
#endif

#ifndef I2S_LOOPBACK
#define I2S_LOOPBACK             (0)
#endif

port p_scl = PORT_I2C_SCL;
port p_sda = PORT_I2C_SDA;
out port p_ctrl = PORT_CTRL;                /* p_ctrl:
                                             * [0:3] - Unused
                                             * [4]   - EN_3v3_N    (1v0 hardware only)
                                             * [5]   - EN_3v3A
                                             * [6]   - EXT_PLL_SEL (CS2100:0, SI: 1)
                                             * [7]   - MCLK_DIR    (Out:0, In: 1)
                                             */

on tile[0]: in port p_margin = XS1_PORT_1G;  /* CORE_POWER_MARGIN:   Driven 0:   0.925v
                                              *                      Pull down:  0.922v
                                              *                      High-z:     0.9v
                                              *                      Pull-up:    0.854v
                                              *                      Driven 1:   0.85v
                                              */

#if ((XUA_SYNCMODE == XUA_SYNCMODE_SYNC || XUA_SPDIF_RX_EN || XUA_ADAT_RX_EN) && !XUA_USE_SW_PLL)
/* Recover external clock using sw_pll by default when using digital Rx or sync mode.
   Use CS2100 if XUA_USE_SW_PLL is set to 0. All other configs used a fixed clock
   generared by sw_pll */
#define USE_FRACTIONAL_N         (1)
#else
#define USE_FRACTIONAL_N         (0)
#endif

#if (USE_FRACTIONAL_N)
#define EXT_PLL_SEL__MCLK_DIR    (0x00)
#else
#define EXT_PLL_SEL__MCLK_DIR    (0x80)
#endif

/* Board setup for XU316 MC Audio (1v1) */
void board_setup()
{
    /* "Drive high mode" - drive high for 1, non-driving for 0 */
    set_port_drive_high(p_ctrl);

    /* Ensure high-z for 0.9v */
    p_margin :> void;

    /* Drive control port to turn on 3V3 and mclk direction appropriately.
     * Bits set to low will be high-z, pulled down */
    p_ctrl <: EXT_PLL_SEL__MCLK_DIR | 0x20;

    /* Wait for power supplies to be up and stable */
    delay_milliseconds(10);
}

/* Working around not being able to extend an unsafe interface (Bugzilla #18670)*/
i2c_regop_res_t i2c_reg_write(uint8_t device_addr, uint8_t reg, uint8_t data)
{
    uint8_t a_data[2] = {reg, data};
    size_t n;

    unsafe
    {
        i_i2c_client.write(device_addr, a_data, 2, n, 1);
    }

    if (n == 0)
    {
        return I2C_REGOP_DEVICE_NACK;
    }
    if (n < 2)
    {
        return I2C_REGOP_INCOMPLETE;
    }

    return I2C_REGOP_SUCCESS;
}

uint8_t i2c_reg_read(uint8_t device_addr, uint8_t reg, i2c_regop_res_t &result)
{
    uint8_t a_reg[1] = {reg};
    uint8_t data[1] = {0};
    size_t n;
    i2c_res_t res;

    unsafe
    {
        res = i_i2c_client.write(device_addr, a_reg, 1, n, 0);

        if (n != 1)
        {
            result = I2C_REGOP_DEVICE_NACK;
            i_i2c_client.send_stop_bit();
            return 0;
        }

        res = i_i2c_client.read(device_addr, data, 1, 1);
    }

    if (res == I2C_ACK)
    {
        result = I2C_REGOP_SUCCESS;
    }
    else
    {
        result = I2C_REGOP_DEVICE_NACK;
    }
    return data[0];
}

/* The number of timer ticks to wait for the audio PLL to lock */
/* CS2100 lists typical lock time as 100 * input period */
#define AUDIO_PLL_LOCK_DELAY        (40000000)

unsafe client interface i2c_master_if i_i2c_client;

void WriteRegs(int deviceAddr, int numDevices, int regAddr, int regData)
{
    i2c_regop_res_t result;

    for(int i = deviceAddr; i < (deviceAddr + numDevices); i++)
    {
        unsafe
        {
            result = i2c_reg_write(i, regAddr, regData);
        }
        assert(result == I2C_REGOP_SUCCESS && msg("I2C write reg failed"));
    }
}



/* Configures the external audio hardware at startup */
void AudioHwInit()
{
    // i2c_regop_res_t result;

    // Wait for power supply to come up.
    delay_milliseconds(100);

    /* Wait until global is set */
    unsafe
    {
        while(!(unsigned) i_i2c_client);
    }

}

/* Configures the external audio hardware for the required sample frequency */
void AudioHwConfig(unsigned samFreq, unsigned mClk, unsigned dsdMode, unsigned sampRes_DAC, unsigned sampRes_ADC)
{
    delay_milliseconds(3);  // Wait for mute to take effect. This takes 104 samples, this is 2.4ms @ 44.1kHz. So lets say 3ms to cover everything.
    // WriteAllDacRegs(PCM5122_STANDBY_PWDN,   0x00); // Set DAC in run mode (no standby or powerdown)
    delay_milliseconds(1);
    // WriteAllDacRegs(PCM5122_MUTE,           0x00); // Un-mute both channels
}

