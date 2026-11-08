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

/* C and XC give a bare '*' different pointer kinds, so xcc refuses to link the
 * pair. The diagnostic is explicit:
 *     Type of symbol csd_ups_frame has mismatch with previous definition
 *     found:    unsigned pointer, C kind, in csd_ups_channel.c
 *     previous: unsigned pointer, XC kind, in csd_dsp.xc
 *     Did you mean to use *unsafe in the xc code to match the * in C?
 * The implementation is C, so a declaration seen by an XC translation unit has
 * to spell the pointer the way C does. One macro, both languages, one spelling
 * of every prototype. */
#ifdef __XC__
#  define CSD_UPS_CPTR * unsafe
#else
#  define CSD_UPS_CPTR *
#endif

/* Host stream rate, in Hz. A change resets all filter history. */
void csd_ups_set_input_rate(unsigned hz);

/* The output rate this input rate must be upsampled to: 384000 for the 48
 * series (48/96/192 kHz), 352800 for the 44.1 series (44.1/88.2/176.4 kHz), 0
 * if the rate belongs to neither family. MCLK is the only thing that differs
 * between families -- the filters and their coefficients are identical -- so
 * the board code should take its clock choice from here rather than from a
 * private copy of the table. */
unsigned csd_ups_expected_out_hz(unsigned in_hz);

/* Confirm the real output clock, as configured by the board. Enables the path
 * only when out_hz is the family rate for the current input rate; a mismatch
 * (for example 44.1 kHz material armed at 384 kHz) disables the path and
 * clears state rather than running at a non-integer ratio. */
void csd_ups_arm(unsigned out_hz);

/* Clear filter history and the delivery queues. */
void csd_ups_reset(void);

/* Process one stereo frame in place. s[0]=L, s[1]=R, Q31.
 * Returns 1 if the frame was rewritten, 0 if it passed through untouched. */
int csd_ups_frame(unsigned CSD_UPS_CPTR s);

/* Diagnostics for the SCPI/MIDI surface. */
unsigned csd_ups_diag_state(void);      /* 1 = armed and active */
unsigned csd_ups_diag_frames(void);     /* frames rewritten this stream */
unsigned csd_ups_diag_in_hz(void);
unsigned csd_ups_diag_out_hz(void);

/* Runtime option (default 0 = off). Enabling is only half of it: the codec
 * must also be told to bypass its own oversampling FIR, and the board refuses
 * that unless the frame clock really is 384 kHz or 352.8 kHz. Both halves are
 * driven together by DIAG:UPSAMPLing:STATe. */
void     csd_ups_set_enabled(unsigned enable);   /* 'on' is a reserved word in XC */
unsigned csd_ups_is_enabled(void);
unsigned csd_ups_diag_refusals(void);   /* enable attempts while not armed */

#ifdef __cplusplus
}
#endif
#endif /* __csd_upsample_hook_h__ */
