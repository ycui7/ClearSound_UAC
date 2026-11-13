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

/** Host kernel aperture (DIAG:DSP:COEFFicient, 2026-11-10 owner directives).
 *  The ONLY write path is the base64 KERNEL CONTAINER: one command carries
 *  one whole kernel and applies it in ONE atomic adoption -- there is no
 *  per-tap/per-index write form and no partial fill. Container layout
 *  (little-endian, inside the base64 payload): header id:u32, taps:u32,
 *  width:u16, q_shift:u16 (12 bytes) followed by taps * width/8 bytes of
 *  coefficients. The aperture validates id, taps, width, q and the total
 *  length against the target table (csd_dsp_kernel_max_taps_for) BEFORE
 *  publishing; nothing moves on a rejection.
 *
 *  The SHIPPED DEFAULT kernel is the unity biquad, so preset >= 2 is
 *  transparent out of the box. The EQ task adopts publications through the
 *  same pause/config/resync/engage path a PRESet change drives
 *  (csd_dsp_eq_task polls csd_dsp_bq_coef_seq beside cfg_seq). Volatile
 *  contract: a reboot, any PRESet write, or a real sample-rate change
 *  restores the shipped default. Task-applied truth (0 shipped, 1 override)
 *  is INFO? eq_kernel_src. All entry points run on the diagnostic thread
 *  except restore (also the audio thread, on a real rate change) and
 *  coef_seq (EQ task poll). */
#define CSD_KERNEL_ID_BIQUAD   1u    /* 5 coeffs [b0,b1,b2,-a1,-a2], Q1.30   */
#define CSD_KERNEL_ID_FIR      2u    /* exactly CSD_KERNEL_FIR_TAPS coeffs,
                                      * Q1.30 (a short container is refused)  */
#define CSD_KERNEL_HDR_BYTES  12u
#define CSD_KERNEL_WIDTH_BITS 32u
#define CSD_KERNEL_Q_SHIFT    30u
/* Container capacity: physical size of every kernel store. The ADDRESSABLE
 * limit is csd_dsp_kernel_max_taps_for(id) per target -- DIAG:DSP:COEFFicient:
 * FORMat? publishes both, derived from these constants, never hand-typed.
 * Physical == addressable ON PURPOSE (2026-11-10): the old 4096 was 4x dead
 * DP RAM across k_live/fir_hist/k_word/kstage and blew the 64 KB
 * DP-relative16 link reach (measured: R_XCORE1_DP_REL16 out of range at
 * 0x10bdf).
 * History (2026-11-10, capacity restore -- MEASUREMENTS IN
 * docs/status-2026-11-10.md): a 1024-tap build DID regress the ADC1 return
 * (0/10 exact vs PRESet 0 8/8 exact, same image; bit-timed loop instructions
 * identical), but the cause was placement, not capacity: the three stores
 * sat in the middle of tile[1]'s DP buckets and their growth shifted every
 * hot per-frame object (EQ ring, upsampler state) by up to 25 KB of banks.
 * They now live in their own tail-most bucket (.dp.bss.large,
 * csd_dsp_store.c) where growing them cannot touch the bit-timed loop's
 * layout. The issue-slot argument that justified 16 taps bound the FIR only
 * while it is SCALAR and only against the frame budget of the CORE that runs
 * it; the FIR arm runs on the EQ task's own core (csd_dsp_eq_task, tile[1]),
 * where the budget is that core's ~2083 cycles/frame -- the scalar engine's
 * affordable tap count is MEASURED (eq_ticks_max, status doc), not argued. */
/* CSD_KERNEL_FIR_TAPS is the COMPILE-TIME CEILING -- it sizes every kernel
 * store (k_word/k_live/fir_hist), so it must be the LARGEST tap count any host
 * rate can afford. It is NOT permission: what the aperture accepts is the
 * MEASURED per-rate limit below, published by DIAG:DSP:COEFFicient:FORMat?
 * and enforced by csd_dsp_kernel_rate_limit().
 *
 * WHY PER-RATE (owner requirement 2026-11-10: "a kernel must be as long as it
 * needs to be"). The FIR is a scalar loop on the EQ task's own core, so its
 * cost per frame is proportional to taps while the frame budget is 1/rate:
 * 48 kHz has 8x the budget of 384 kHz, 96 kHz 4x, 192 kHz 2x. Capping every
 * rate at what 384 kHz can afford threw away 87 % of the compute available at
 * 48 kHz. The limits below are therefore set from MEASURED eq_ticks_max per
 * (taps, rate) on this board -- see docs/status-2026-11-10.md for the table,
 * the fit and the margin argument. REVERT to a single number by deleting the
 * table and returning CSD_KERNEL_FIR_TAPS from csd_dsp_kernel_rate_limit(). */
#ifndef CSD_KERNEL_FIR_TAPS
#define CSD_KERNEL_FIR_TAPS 128u          /* = the 48 kHz limit (largest tier) */
#endif
#define CSD_KERNEL_MAX_TAPS CSD_KERNEL_FIR_TAPS

/* Measured per-rate FIR ceilings (largest tap count whose measured worst-frame
 * burst stays inside CSD_KERNEL_FIR_BUDGET_FRACTION of the frame period). Each
 * tier is measured at the FASTER member of the family, so applying it to the
 * slower one (44.1k under 48k, 352.8k under 384k) is conservative. */
#define CSD_KERNEL_FIR_TAPS_AT_48   128u
#define CSD_KERNEL_FIR_TAPS_AT_96    64u
#define CSD_KERNEL_FIR_TAPS_AT_192   32u
#define CSD_KERNEL_FIR_TAPS_AT_384   16u

void csd_dsp_kernel_publish(unsigned id, unsigned taps, const int32_t *coef);
unsigned csd_dsp_kernel_max_taps_for(unsigned id);   /* 0 = unknown target   */
/* Legal tap count for the CURRENT stream rate: the aperture's ceiling. The
 * physical ceiling is csd_dsp_kernel_max_taps_for(); a container between the
 * two is well-formed but cannot be computed in the frame budget, and the
 * aperture refuses it with -222 rather than adopt a kernel that would overrun
 * the EQ task. Both are derived from the table above -- FORMat? publishes
 * them, so validator, store and descriptor cannot drift. */
unsigned csd_dsp_kernel_rate_limit(unsigned id);
unsigned csd_dsp_kernel_rate_limit_rate_hz(void);    /* rate the limit is for  */
unsigned csd_dsp_kernel_taps_legal(unsigned id, unsigned tp); /* both gates    */
unsigned csd_dsp_kernel_container_max_taps(void);
unsigned csd_dsp_kernel_request_id(void);
unsigned csd_dsp_kernel_request_taps(void);
unsigned csd_dsp_kernel_request_en(void);
void csd_dsp_bq_coef_restore(void);
int32_t csd_dsp_bq_coef_read(unsigned idx);          /* debug/query-only word */
unsigned csd_dsp_bq_coef_seq(void);
unsigned csd_dsp_bq_diag_kernel_src(void);

/** Apply the compile-time default preset (-DCSD_DSP_PRESET_DEFAULT=n); no-op otherwise. Call once at boot. */
void csd_dsp_preset_boot(void);

#endif
