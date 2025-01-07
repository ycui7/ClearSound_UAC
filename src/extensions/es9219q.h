#ifndef ES9219Q_H_
#define ES9219Q_H_

#define ES9219Q_SYNC_I2C_DEVICE_ADDR        (0x94>>1)     //for setting up PLL before system clock is available. 
#define ES9219Q_I2C_DEVICE_ADDR             (0x90>>1)


//	Mnemonic constants 
//	Values must be shifted by bit per datasheet to work. 
#define ES9219Q_AO_SOFT_RESET		(1)
#define ES9219Q_PLL_CP_PDB			(1)		//0: Disabled, 1: Enabled. 
#define ES9219Q_PLL_VCO_PDB			(1)		//0: Disabled, 1: Enabled. 
#define ES9219Q_PLL_VCOP_BIAS_SEL	(0)		// Vtune/45kohm default
#define ES9219Q_PLL_VCO_BIAS_SEL	(0)		// 2.0uA default
#define ES9219Q_PLL_BYPASS			(0)		// ! default value undocumented
#define ES9219Q_PLL_LOW_BW			(0)		// ! default value undocumented
#define ES9219Q_PLL_CLK_IN_DIV		(2)		//for 24.xxxMHZ MCLK
#define ES9219Q_PLL_CLK_FB_DIV		(65536)	//2^22 undocumented in datasheet, acquired from ES9020 datasheet 
#define ES9219Q_PLL_PFD_DELAY_SEL	(0)		//8.5ns default
#define ES9219Q_PLL_CP_BIAS_SEL		(0b011)	//4uA default
#define ES9219Q_PLL_CLK_OUT_DIV		(4)		//divide by 4
#define ES9219Q_PLL_HV_REG_BYPASS	(0)		//0: Normal, 1: AVDD
#define ES9219Q_PLL_LV_REG_BYPASS	(0)		//0: Normal, 1: DVCC
#define ES9219Q_PLL_HV_REG_PDB		(0) 	//0: SHUNT to GROUND, 1: Normal
#define ES9219Q_PLL_LV_REG_PDB		(0)		//0: SHUNT to GROUND, 1: Normal
#define ES9219Q_PLL_VREF_SEL		(0)		//0: 2V, 1: 1.9V, 2: 1.8V, 3: 1.7V
#define ES9219Q_SEL_DAC_CLKIN		(0)		//0: XTAL, 1: DAC
#define ES9219Q_PD_XTAL				(1)		//0: ON, 1: OFF
#define ES9219Q_PLL_INPUT_CLK		(1)		//0: NONE, 1: MCLK, 2: BCK, 3: XTAL


//Register Addresess
#define ES9219Q_SOFT_RESET      (0xC0) // Reg 192 
#define ES9219Q_PLL_CONFIG1     (0xC1) // Reg 193 
#define ES9219Q_PLL_CONFIG2A    (0xC2) // Reg 194
#define ES9219Q_PLL_CONFIG2B    (0xC3) // Reg 195
#define ES9219Q_PLL_CONFIG2C    (0xC4) // Reg 196 
#define ES9219Q_PLL_CONFIG2D    (0xC5) // Reg 197 
#define ES9219Q_PLL_CONFIG3A    (0xC6) // Reg 198 
#define ES9219Q_PLL_CONFIG3B    (0xC7) // Reg 199 
#define ES9219Q_PLL_CONFIG4     (0xC8) // Reg 200
#define ES9219Q_PLL_CONFIG5     (0xC9) // Reg 200

#define ES9219Q_SYSTEM_REGISTERS				(0x00)
#define ES9219Q_INPUT_SELECT					(0x01)
#define ES9219Q_MIXING_N_AUTOMUTE_CONFIG		(0x02)
#define ES9219Q_ANALOG_VOL_CTRL					(0x03)
#define ES9219Q_DOP_N_VOL_RAMP_RATE				(0x06)
#define ES9219Q_FILTER_SHAPE_N_SYSTEM_MUTE		(0x07)
#define ES9219Q_GPIO12_CONFIG					(0x08)
#define ES9219Q_MASTER_MODE_N_SYNC_CONFIG		(0x0A)
#define ES9219Q_THD_BYPASS_N_MONO_MODE			(0x0D)
#define ES9219Q_SOFT_START_CONFIG				(0x0E)
#define ES9219Q_VOL_CTRL_LO						(0x0F)
#define ES9219Q_VOL_CTRL_HI						(0x10)
#define ES9219Q_MASTER_TRIM_BYTE0				(0x11)
#define ES9219Q_MASTER_TRIM_BYTE1				(0x12)
#define ES9219Q_MASTER_TRIM_BYTE2				(0x13)
#define ES9219Q_MASTER_TRIM_BYTE3				(0x14)
#define ES9219Q_GPIO_INPUT_SELECT				(0x15)
#define ES9219Q_THD_COMP_C2_LO					(0x16)
#define ES9219Q_THD_COMP_C2_HI					(0x17)
#define ES9219Q_THD_COMP_C3_LO					(0x18)
#define ES9219Q_THD_COMP_C3_HI					(0x19)
#define ES9219Q_GENERAL_CONFIG					(0x1B)
#define ES9219Q_GPIO_CONFIG_N_AUTO_CLK_GEAR		(0x1D)
#define ES9219Q_CHARGE_PUMP_CLOCK_CONFIG_LO		(0x1E)

#define ES9219Q_CHARGE_PUMP_CLOCK_CONFIG_HI		(0x1F)
#define ES9219Q_CP_CLK_SEL							(0b00 << 6)
#define ES9219Q_CP_CLK_EN							(0b11 << 4)
#define ES9219Q_CP_CLK_DIV							(64)

#define ES9219Q_AMP_CONFIG						(0x20)
#define ES9219Q_AMP_PDB_SS							(0 << 7)	//0: control by amp_mode
#define ES9219Q_AMP_MODE_GPIO						(2 << 3)	//0: core on, 1: LowFi, 2: HiFi 1V, 3: HiFi 2V

#define ES9219Q_FIR_RAM_ADDR					(0x28)
#define ES9219Q_FIR_RAM_DATA_BYTE0				(0x29)
#define ES9219Q_FIR_RAM_DATA_BYTE1				(0x2A)
#define ES9219Q_FIR_RAM_DATA_BYTE2				(0x2B)
#define ES9219Q_FIR_CONFIG						(0x2C)

#define ES9219Q_ANALOG_CTRL_OVERRIDE_1			(0x2D)
#define ES9219Q_ENAUX								(0 << 7)	//1: external analog input, DAC off
#define ES9219Q_AREG_PDB							(1 << 6)	//1: analog regulator ON
#define ES9219Q_ENPHA								(1 << 5)	//1: preamplifier ON
#define ES9219Q_CPH_ENS								(1 << 4)	//1: main charge pump is ON. (encp_on must be set to 1b1 for it to work)
#define ES9219Q_CPH_ENW								(0 << 3)	//1: Enable Main charge pump in weak mode. 
#define ES9219Q_CPH_APDB							(1 << 2)	//1: analog VREF on. 
#define ES9219Q_CP_CLKIO_SEL						(0 << 2)	//0: internal oscillator for charge pump, 1: digitally generated clock

#define ES9219Q_ANALOG_CTRL_OVERRIDE_2			(0x2E)
#define ES9219Q_DIG_OVER_EN							(0 << 7) 	//0: use register 0x2D, 1: use digital core
#define ES9219Q_SEL1V								(0 << 2) 	//0: use internal digital regulator, 1: disable internal regulator
#define ES9219Q_SHTOUTB								(1 << 1) 	//0: shunt on amplifier output
#define ES9219Q_SHTINB								(1 << 1) 	//0: shunt on amplifier input

#define ES9219Q_ANALOG_CTRL_OVERRIDE_3			(0x2F)
#define ES9219Q_ENFCB								(0 << 7)	//0: fast AREF charging
#define ES9219Q_ENCP_OE								(1 << 6)	//0: GPIO2 controls charge pump, 1: digital core
#define ES9219Q_ENAUX_OE							(1 << 5)	//0: GPIO2 controls charge pump, 1: digital core
#define ES9219Q_CPL_ENS								(1 << 4)	//1: main charge pump is ON. (encp_on must be set to 1b1 for it to work)
#define ES9219Q_CPL_ENW								(0 << 3)	//1: Enable Main charge pump in weak mode. 
#define ES9219Q_SEL3V3_PS							(0 << 2)	//0: use 1.8V for output stage, 1: use 3.3V for output stage
#define ES9219Q_ENSM_PS								(0 << 1)	//0: allow smooth transisition from 1.8V to 3.3V 1: normal operation
#define ES9219Q_SEL3V3_CPH							(0 << 0)	//0: use 1.8V for charge pump, 1: use 3.3V for charge pump

#define ES9219Q_ANALOG_CTRL_SIGNALS				(0x30)
#define ES9219Q_THD_COMP_C2_CH2_LO				(0x35)
#define ES9219Q_THD_COMP_C2_CH2_HI				(0x36)
#define ES9219Q_THD_COMP_C3_CH2_LO				(0x37)
#define ES9219Q_THD_COMP_C3_CH2_HI				(0x38)
#define ES9219Q_CHIP_STATUS						(0x40)
#define ES9219Q_GPIO_READBACK					(0x41)
#define ES9219Q_READ_INPUT_SEL_N_AUTOMUTE_STAT	(0x48)
#define ES9219Q_READ_FIR_RAM_DATA_BYTE0			(0x49)
#define ES9219Q_READ_FIR_RAM_DATA_BYTE1			(0x4A)
#define ES9219Q_READ_FIR_RAM_DATA_BYTE2			(0x4B)
#define ES9219Q_READ_LOCK_STATUS				(0x4D)

#define ES9219Q_CROSSTALK_COMP_CONFIG			(0x80)
#define ES9219Q_BYPASS_CT							(1 << 7)	//1: disable, 0: Enable
#define ES9219Q_ENABLE_PLL_LOCK						(1 << 6)	//1: Lock Status indicate both APLL and DPLL

#define ES9219Q_CROSSTALK_COMP_SCALE_CH1_LO		(0x82)
#define ES9219Q_CROSSTALK_COMP_SCALE_CH1_HI		(0x83)
#define ES9219Q_CROSSTALK_COMP_SCALE_CH2_LO		(0x84)
#define ES9219Q_CROSSTALK_COMP_SCALE_CH2_HI		(0x85)

#define ES9219Q_ANALOG_CTRL_N_I2S_MON_CONFIG	(0x86)
#define ES9219Q_CH1_ANALOG_SWAP						(1 << 3)	//0: normal, 1: inverted
#define ES9219Q_CH2_ANALOG_SWAP						(1 << 2)	//0: normal, 1: inverted

#define ES9219Q_ANALOG_CTRL_OVERRIDE_N_ATR		(0x87)
#define ES9219Q_DISALBE_ATR_CH2						(0 << 6)	//0: THD Comp Enabled		
#define ES9219Q_DISALBE_ATR_CH1						(0 << 5)	//0: THD Comp Enabled	
#define ES9219Q_PDB_ATR_R							(1 << 1)	//1: Enabled 	
#define ES9219Q_PDB_ATR_L							(1 << 0)	//1: Enabled 

#endif /* ES9219Q_H_ */