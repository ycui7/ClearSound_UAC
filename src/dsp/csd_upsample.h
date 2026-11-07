/* CSD upsampling DSP core -- portable C, no XMOS or RTOS dependency.
 *
 * Stage A  sinc polyphase interpolator: 48/96/192 kHz -> 384 kHz
 * Stage B  linear-phase 4th-order-equivalent band limit, corner 15 kHz
 *
 * Coefficients live in csd_dsp_taps.h, generated and property-tested by
 * tools/dsp/gen_upsample_taps.py. Nothing here allocates: every state is a
 * plain struct so it can be a C static on an XS3 tile.
 */
#ifndef CSD_UPSAMPLE_H
#define CSD_UPSAMPLE_H

#include <stdint.h>
#include "csd_dsp_taps.h"

/* ---- stage A: polyphase sinc interpolator ---------------------------- */
#define CSD_UP_MAX_TAPS_PER_PHASE 64u

typedef struct {
    int32_t hist[CSD_UP_MAX_TAPS_PER_PHASE];  /* input history, newest first */
    uint32_t n_hist;                          /* taps per phase in use       */
} csd_up_state;

/* Output rate is always CSD_DSP_FS_OUT. Returns the integer ratio (8/4/2),
 * or 0 when the rate is unsupported -- never a silent wrong answer. */
uint32_t csd_up_ratio(uint32_t fs_in);

void csd_up_reset(csd_up_state *s, uint32_t fs_in);

/* n_in input frames -> n_in * ratio output frames. out must have room. */
void csd_up_process(csd_up_state *s, uint32_t fs_in,
                    const int32_t *in, uint32_t n_in, int32_t *out);

/* ---- stage B: band limit / noise shaping ---------------------------- */
typedef struct {
    int32_t hist[CSD_BL_TAPS];
    uint32_t pos;
} csd_bl_state;

void csd_bl_reset(csd_bl_state *s);
void csd_bl_process(csd_bl_state *s, const int32_t *in, uint32_t n, int32_t *out);

/* Group delay of the band-limit stage, in 384 kHz samples (linear phase). */
#define CSD_BL_GROUP_DELAY CSD_BL_DELAY


/* ---- stage A, structural form: 2x halfband cascade ------------------- */
#define CSD_HB_HIST 64u

typedef struct {
    int32_t hist[CSD_HB_HIST];   /* input history, newest at [0] */
} csd_hb_state;

void csd_hb_reset(csd_hb_state *s);
/* n_in frames -> 2*n_in frames. Even outputs cost one multiply (centre tap);
 * odd outputs skip the 62 structural zeros and fold the symmetric remainder:
 * 33 multiplies per input sample instead of 127. */
void csd_hb2_process(csd_hb_state *s, const int32_t *in, uint32_t n_in, int32_t *out);

/* Cascade: 192 kHz one 2x stage, 96 kHz two, 48 kHz three. BOTH scratch buffers
 * must hold 4*n_in samples. Two are needed because the 48 kHz path expands
 * 8x: writing a stage forward in-place would destroy samples it has not read. */
typedef struct {
    csd_hb_state st[3];
} csd_cascade_state;

void csd_cascade_reset(csd_cascade_state *c);
void csd_cascade_process(csd_cascade_state *c, uint32_t fs_in,
                         const int32_t *in, uint32_t n_in,
                         int32_t *out, int32_t *tmp, int32_t *tmp2);

#endif /* CSD_UPSAMPLE_H */
