/* Audio measurement probes for the analyzer-mode diagnostic interface.
 *
 * Both "channels in" and "channels out" are measured from buffers that
 * already exist in the audio path -- nothing is synthesised for a reply:
 *
 *   OUT1/OUT2  = the DAC-bound frame as seen by UserBufferManagement BEFORE
 *                the DSP (what the device is really presenting to the line out)
 *   IN1/IN2    = the ADC-bound frame (what is really coming back in)
 *
 * Writer: the audiohub core, once per frame. Reader: the diagnostic core.
 * Handoff is a two-slot snapshot so a reader never sees a half-updated set,
 * which matters: a torn read here would print a plausible-but-wrong dB value,
 * the exact failure mode that makes a diagnostic interface untrustworthy.
 *
 * Cost discipline: this runs on the thread that must meet the LRCLK deadline.
 * It is integer-only, no divisions, no libm: one shifted square, one compare,
 * one sign test per channel per frame. */
#ifndef __csd_meas_h__
#define __csd_meas_h__

#include <stdint.h>

#define CSD_MEAS_CH           4      /* 0,1 = line out (source), 2,3 = line in */
#define CSD_MEAS_WINDOW       256u   /* samples per measurement window */

typedef struct {
    uint32_t sumsq;         /* sum of (x>>8)^2 over the window, Q15^2 scaled */
    uint32_t peak;          /* max |x| in window, Q31 magnitude */
    uint32_t crossings;     /* sign transitions with hysteresis, in window */
    uint32_t frames;        /* windows completed since reset (aging) */
    int32_t  last_state;    /* 1 / -1 / 0 for hysteresis */
    uint8_t  valid;         /* window completed at least once */
} csd_meas_ch_t;

typedef struct {
    csd_meas_ch_t ch[CSD_MEAS_CH];
    uint32_t hz;            /* sample rate the numbers belong to */
} csd_meas_snap_t;

/* --- writer side (audiohub core) --- */
void csd_meas_reset(void);
void csd_meas_set_rate(uint32_t hz);
void csd_meas_out(int32_t l, int32_t r);   /* pre-DSP DAC-bound frame */
void csd_meas_in (int32_t l, int32_t r);   /* ADC-bound frame */

/* --- reader side (diagnostic core) ---
 * Returns a pointer to the published snapshot, or NULL if no window has
 * completed yet -- callers MUST surface that as an error, never as zeroes. */
const csd_meas_snap_t *csd_meas_snapshot(void);

/* Integer helpers (no libm). rms of one channel, Q31 magnitude, 0 if invalid.
 * freq_hz is crossings/2 per window scaled by rate/window; 0 if undetermined. */
uint32_t csd_meas_rms_q31(const csd_meas_ch_t *c);
uint32_t csd_meas_freq_hz(const csd_meas_ch_t *c, uint32_t hz);

#endif
