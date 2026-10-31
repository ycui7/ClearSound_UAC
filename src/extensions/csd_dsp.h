// CSD PCM in-line DSP — public API.
// Compiled/linked only when the app defines PCM_DSP_EN=1 (see xua_defs.h,
// tools/dsp/test_gates.sh). Design + coefficient provenance:
// tools/dsp/gen_coeffs.py; plan ~/.hermes/plans/2026-10-29_110000-csd-pcm-dsp-p3.md
#ifndef __csd_dsp_h__
#define __csd_dsp_h__

#include <stdint.h>

/** Process one just-filled DAC output frame (NUM_USB_CHAN_OUT == 2).
 *  Called from the audiohub sample-exchange BEFORE the frame reaches I2S.
 *  s[] holds linear PCM two's-complement words (bit order as received). */
void csd_dsp_frame(unsigned *s);

/** (Re)select the tap table for a sample rate (Hz, nearest supported wins).
 *  Resets state; call on every sample-rate change. */
void csd_dsp_set_rate(unsigned hz);

/** Inform the DSP of codec/DSD mode (0 == PCM). Non-zero fully bypasses. */
void csd_dsp_set_dsd(unsigned dsdMode);
/* Raw DoP/DSD views for the generator rig (informational only). */
unsigned csd_dsp_diag_dsd_flag(void);
unsigned csd_dsp_diag_dop_latched(void);

/** Presets: 0 OFF (byte-exact), 1 DELAY-only (linear-phase group delay, no
 *  boost — A3 structure validation), >=2 FIR shelf (B1). */
void csd_dsp_set_preset(unsigned p);
unsigned csd_dsp_get_preset(void);

/** Apply the compile-time default preset (-DCSD_DSP_PRESET_DEFAULT=n); no-op otherwise. Call once at boot. */
void csd_dsp_preset_boot(void);

#endif
