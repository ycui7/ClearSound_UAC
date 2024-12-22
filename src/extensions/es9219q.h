#ifndef ES9219Q_H_
#define ES9219Q_H_

#define ES9219Q_I2C_DEVICE_ADDR      (0x90)

#define ES9219Q_DEVICE_CONTROL       (0x02)
#define ES9219Q_DEVICE_CONFIG_1      (0x03)
#define ES9219Q_GLOBAL_CONFIG        (0x05)
#define ES9219Q_RATIO_1              (0x06)
#define ES9219Q_RATIO_2              (0x07)
#define ES9219Q_RATIO_3              (0x08)
#define ES9219Q_RATIO_4              (0x09)
#define ES9219Q_FUNC_CONFIG_1        (0x16)
#define ES9219Q_FUNC_CONFIG_2        (0x17)

//Register Addresess
// Page 0
#define ES9219Q_PAGE_CTRL     0x00 // Register 0  - Page Control
#define ES9219Q_SW_RST        0x01 // Register 1  - Software Reset
#define ES9219Q_NDAC          0x0B // Register 11 - NDAC Divider Value
#define ES9219Q_MDAC          0x0C // Register 12 - MDAC Divider Value
#define ES9219Q_DOSR          0x0E // Register 14 - DOSR Divider Value (LS Byte)
#define ES9219Q_NADC          0x12 // Register 18 - NADC Divider Value
#define ES9219Q_MADC          0x13 // Register 19 - MADC Divider Value
#define ES9219Q_AOSR          0x14 // Register 20 - AOSR Divider Value
#define ES9219Q_CODEC_IF      0x1B // Register 27 - CODEC Interface Control
#define ES9219Q_DAC_SIG_PROC  0x3C // Register 60 - DAC Sig Processing Block Control
#define ES9219Q_ADC_SIG_PROC  0x3D // Register 61 - ADC Sig Processing Block Control
#define ES9219Q_DAC_CH_SET1   0x3F // Register 63 - DAC Channel Setup 1
#define ES9219Q_DAC_CH_SET2   0x40 // Register 64 - DAC Channel Setup 2
#define ES9219Q_DACL_VOL_D    0x41 // Register 65 - DAC Left Digital Vol Control
#define ES9219Q_DACR_VOL_D    0x42 // Register 66 - DAC Right Digital Vol Control
#define ES9219Q_ADC_CH_SET    0x51 // Register 81 - ADC Channel Setup
#define ES9219Q_ADC_FGA_MUTE  0x52 // Register 82 - ADC Fine Gain Adjust/Mute

// Page 1
#define ES9219Q_PWR_CFG       0x01 // Register 1  - Power Config
#define ES9219Q_LDO_CTRL      0x02 // Register 2  - LDO Control
#define ES9219Q_PLAY_CFG1     0x03 // Register 3  - Playback Config 1
#define ES9219Q_PLAY_CFG2     0x04 // Register 4  - Playback Config 2
#define ES9219Q_OP_PWR_CTRL   0x09 // Register 9  - Output Driver Power Control
#define ES9219Q_CM_CTRL       0x0A // Register 10 - Common Mode Control
#define ES9219Q_HPL_ROUTE     0x0C // Register 12 - HPL Routing Select
#define ES9219Q_HPR_ROUTE     0x0D // Register 13 - HPR Routing Select
#define ES9219Q_HPL_GAIN      0x10 // Register 16 - HPL Driver Gain
#define ES9219Q_HPR_GAIN      0x11 // Register 17 - HPR Driver Gain
#define ES9219Q_HP_START      0x14 // Register 20 - Headphone Driver Startup
#define ES9219Q_LPGA_P_ROUTE  0x34 // Register 52 - Left PGA Positive Input Route
#define ES9219Q_LPGA_N_ROUTE  0x36 // Register 54 - Left PGA Negative Input Route
#define ES9219Q_RPGA_P_ROUTE  0x37 // Register 55 - Right PGA Positive Input Route
#define ES9219Q_RPGA_N_ROUTE  0x39 // Register 57 - Right PGA Negative Input Route
#define ES9219Q_LPGA_VOL      0x3B // Register 59 - Left PGA Volume
#define ES9219Q_RPGA_VOL      0x3C // Register 60 - Right PGA Volume
#define ES9219Q_ADC_PTM       0x3D // Register 61 - ADC Power Tune Config
#define ES9219Q_AN_IN_CHRG    0x47 // Register 71 - Analog Input Quick Charging Config
#define ES9219Q_REF_STARTUP   0x7B // Register 123 - Reference Power Up Config

#endif /* TLV320ES9219Q_H_ */