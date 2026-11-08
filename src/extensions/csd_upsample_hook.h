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

/* ---- wire-rate contract (owner directive 2026-11-08: run at MCLK/128) ----
 *
 * UserBufferManagement is called once per HOST PAIR at the stream rate, but
 * the I2S slots advance at the wire rate (LRCLK = MCLK/128, 384 kHz). One
 * call cannot span that gap -- writing 8x words into a host-cadence buffer
 * was the bug that garbled the wire. Two calls close it:
 *
 *   csd_ups_host_pair(l, r) -- one host pair IN, at host-pair cadence.
 *       Runs stage A (halfband cascade) and pushes `ratio` outputs per
 *       channel into the delivery queues. The frame is left untouched.
 *
 *   csd_ups_slot(ch, w)     -- one wire slot OUT, at LRCLK cadence. Pops one
 *       output for channel ch, band-limits it (stage B), returns the word.
 *       Returns w unchanged when the path is not active.
 *
 *   csd_ups_host_active()   -- 1 when enabled AND armed: the DSP frame path
 *       must stand down so exactly one stream reaches the wire.
 *
 * Words pass by value on purpose: no XC/C pointer-kind disagreement across
 * translation units (the CSD_UPS_CPTR history above), single machine word. */
void     csd_ups_host_pair(int l, int r);
int      csd_ups_slot(unsigned ch, int w);
unsigned csd_ups_host_active(void);

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

/* Persisted intent, held on the audio tile. Written by the config path after it
 * fetches the journal's value over the channel; read by the rate-change force. */
void     csd_ups_set_want(unsigned w);
unsigned csd_ups_get_want(void);

#ifdef __cplusplus
}
#endif
#endif /* __csd_upsample_hook_h__ */
