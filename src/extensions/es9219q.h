#ifndef ES9219Q_H_
#define ES9219Q_H_

#define ES9219Q_SYNC_I2C_DEVICE_ADDR        (0x94)     //for setting up PLL before system clock is available. 
#define ES9219Q_I2C_DEVICE_ADDR             (0x90)

#define ES9219Q_AO_SOFT_RESET		(1)
#define ES9219Q_PLL_CP_PDB			(1)		//0: Disabled, 1: Enabled. 
#define ES9219Q_PLL_VCO_PDB			(1)		//0: Disabled, 1: Enabled. 
#define ES9219Q_PLL_VCOP_BIAS_SEL	(0)		// Vtune/45kohm default
#define ES9219Q_PLL_VCO_BIAS_SEL	(0)		// 2.0uA default
#define ES9219Q_PLL_BYPASS			(0)		// ! default value undocumented
#define ES9219Q_PLL_LOW_BW			(0)		// ! default value undocumented
#define ES9219Q_PLL_CLK_IN_DIV		(2)		//for 24.xxxMHZ MCLK
#define ES9219Q_PLL_CLK_FB_DIV		(4194304)	//undocumented in datasheet, acquired from ES9020 datasheet 
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

#endif /* ES9219Q_H_ */