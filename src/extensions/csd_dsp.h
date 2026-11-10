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
/* Per-frame EQ task: own core, SPSC rings with UserBufferManagement (see the
 * relocation block in csd_dsp.xc). Spawned on tile[1] by USER_MAIN_CORES. */
void csd_dsp_eq_task(void);

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

/** Host-side coefficient aperture (DIAG:DSP:COEFFicient, 2026-11-10): the
 *  single biquad section's five Q1.30 coefficients [b0,b1,b2,-a1,-a2],
 *  index 0..4. The SHIPPED DEFAULT kernel is unity (identity), so preset
 *  >= 2 is transparent out of the box; real kernels are host-loaded. The
 *  EQ task adopts writes through the same config/resync/engage path a
 *  PRESet change drives (csd_dsp_eq_task polls csd_dsp_bq_coef_seq beside
 *  cfg_seq). Volatile contract: a reboot, any PRESet write, or a real
 *  sample-rate change restores the shipped default. write returns 1 on
 *  acceptance; read is the live/request-side value; diag_kernel_src is
 *  the task-applied truth (0 shipped, 1 override) = INFO? eq_kernel_src.
 *  All entry points run on the diagnostic thread except restore (also the
 *  audio thread, on a real rate change) and coef_seq (EQ task poll). */
int csd_dsp_bq_coef_write(unsigned idx, int32_t value);
void csd_dsp_bq_coef_restore(void);
int32_t csd_dsp_bq_coef_read(unsigned idx);
unsigned csd_dsp_bq_coef_seq(void);
unsigned csd_dsp_bq_diag_kernel_src(void);

/** Apply the compile-time default preset (-DCSD_DSP_PRESET_DEFAULT=n); no-op otherwise. Call once at boot. */
void csd_dsp_preset_boot(void);

#endif
