// CSD upsampling hook -- public interface.
//
// Placement rule, from the product requirement: the upsampler REPLACES the
// stream. Exactly one stream reaches the mixer per channel; the host-rate
// samples are never forwarded alongside the upsampled ones, and nothing is
// summed into the output buffer.
//
// Arming contract: csd_ups_arm() must be called with the rate the codec and the
// I2S clocks are ACTUALLY running at. Until that is 384000 the hook passes
// frames through untouched and returns 0, because emitting upsampled words into
// a host-rate LRCLK would be wrong by the ratio (8x at 48 kHz) rather than
// merely degraded.
#ifndef __csd_upsample_hook_h__
#define __csd_upsample_hook_h__

#ifdef __cplusplus
extern "C" {
#endif

/* Host stream rate, in Hz. A change resets all filter history. */
void csd_ups_set_input_rate(unsigned hz);

/* Confirm the real output clock. 384000 enables the path; anything else
 * disables it and clears state. */
void csd_ups_arm(unsigned out_hz);

/* Clear filter history and the delivery queues. */
void csd_ups_reset(void);

/* Process one stereo frame in place. s[0]=L, s[1]=R, Q31.
 * Returns 1 if the frame was rewritten, 0 if it passed through untouched. */
int csd_ups_frame(unsigned *s);

/* Diagnostics for the SCPI/MIDI surface. */
unsigned csd_ups_diag_state(void);      /* 1 = armed and active */
unsigned csd_ups_diag_frames(void);     /* frames rewritten this stream */
unsigned csd_ups_diag_in_hz(void);
unsigned csd_ups_diag_out_hz(void);

#ifdef __cplusplus
}
#endif
#endif /* __csd_upsample_hook_h__ */
