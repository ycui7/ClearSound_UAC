/* Kernel stores -- physically separate translation unit, deliberately.
 *
 * WHY A SEPARATE TU AND A DEDICATED SECTION (2026-11-10, capacity restore).
 * These are the three largest tile[1] DP consumers of the EQ engine: at the
 * contract capacity (CSD_KERNEL_FIR_TAPS=1024) they total 16 KB. When they
 * lived inline in csd_dsp_bq.c that 16 KB sat IN THE MIDDLE of tile[1]'s
 * .dp.bss buckets, and enlarging it from the 16-tap experiment shifted EVERY
 * hot DP object above them -- the csd_eq_ring SPSC words and the software-
 * LRCLK loop's own state -- by up to 0x63e8 bytes (measured, maps of the two
 * images in ~/.hermes/cache/scratch: rin/rout/wr_pub/rd/done/engaged, all
 * per-frame hot on the zero-margin bit-timed core, moved +25 KB; ups_blob
 * +8.4 KB). The bit-timed loop's own instructions are identical between the
 * two images, so the RED regression was layout, not code: the DAC stream
 * shifted 1-3 bit clocks against the free-running ADC1 receiver, regrouping
 * received words (0/10 exact vs PRESet 0 8/8 exact on the same image).
 *
 * Placing them in .dp.bss.large (a linker bucket that sits AFTER every other
 * DP bucket -- _fdp.bss.large == _edp.bss in the pre-fix map, i.e. empty and
 * tail-most) means growing these stores can never again perturb the banks of
 * a hot object: everything else lands exactly where it landed before, and
 * the growth happens at the tail of tile[1]'s static area. Still DP-relative
 * addressable: the whole bucket is < 64 K words from _dp, so the R_XCORE1_-
 * DP_REL16 encoding still covers it (measured 47 K words at 1024 taps; the
 * 4096-tap experiment failed at 68 K words).
 *
 * Visibility contract is unchanged (see the aperture block in csd_dsp_bq.c):
 * k_word is written by the diagnostic thread and read by the EQ task, so it
 * stays volatile word-aligned 32-bit; k_live/fir_hist are task-owned. */
#include <stdint.h>
#include "csd_dsp.h"

#define CSD_KSTORE __attribute__((section(".dp.bss.large"), aligned(8)))

/* Host-composed kernel (diagnostic-thread writes, task double-reads). */
CSD_KSTORE volatile int32_t  csd_k_word[CSD_KERNEL_MAX_TAPS];
/* Task-owned staging copy of the adopted kernel + FIR history. */
CSD_KSTORE int32_t  csd_k_live[CSD_KERNEL_MAX_TAPS];
CSD_KSTORE int32_t  csd_fir_hist[2][CSD_KERNEL_MAX_TAPS];
