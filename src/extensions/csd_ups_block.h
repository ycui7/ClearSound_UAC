/* CSD upsampling engine -- public interface.
 *
 * The upsampler IS the DSP: this is the interpolating role of the
 * fd_block_fir block engine (see the header comment in csd_ups_block.c).
 *
 * Placement rule, from the product requirement: the upsampler REPLACES the
 * stream. Exactly one stream reaches the mixer per channel; the host-rate
 * samples are never forwarded alongside the upsampled ones, and nothing is
 * summed into the output buffer.
 *
 * Arming contract: csd_ups_note_wire_rate() must be called with the rate the
 * codec and the I2S clocks are ACTUALLY running at. Until that is a family
 * rate the engine passes frames through untouched, because emitting
 * upsampled words into a host-rate LRCLK would be wrong by the ratio rather
 * than merely degraded.
 *
 * The four names lib_xua's audiohub links against are a frozen contract
 * (xua_audiohub.xc declares them as externs -- it must not depend on app
 * headers): csd_ups_slot, csd_ups_get_want, csd_ups_is_enabled,
 * csd_ups_expected_out_hz. Everything else is app-internal.
 *
 * Words pass by value on purpose: no XC/C pointer-kind disagreement across
 * translation units, single machine word (the history that forced this:
 * "Type of symbol ... has mismatch with previous definition" for a bare '*'
 * seen as different pointer kinds in the two languages).
 */
#ifndef __csd_ups_block_h__
#define __csd_ups_block_h__

#ifdef __cplusplus
extern "C" {
#endif

/* ---- control plane (audio tile's config path, and SCPI) ---------------- */

/* Host stream rate, Hz. A change restarts the engine (history dropped);
 * while armed, a non-family rate (e.g. the 384 kHz mic re-config) is refused
 * and counted rather than silently disarming a live stream. */
void     csd_ups_note_stream_rate(unsigned hz);

/* Confirm the real wire rate the board configured. Disables the path unless
 * the value belongs to the family of the current host rate. */
void     csd_ups_note_wire_rate(unsigned out_hz);

/* The family output rate for this input rate: 384000 for the 48 series,
 * 352800 for the 44.1 series, 0 for anything else. One source of truth,
 * read by the board's MCLK force, lib_xua's fabric decision, and the engine's
 * own arming validation. */
unsigned csd_ups_expected_out_hz(unsigned in_hz);

/* Persisted intent vs codec grant. want is what the journal/operator asked
 * for; enabled is what the codec ACCEPTED (its own oversampling FIR
 * bypassed). Enabling is only half of it -- the path still has to be armed
 * at a family output rate or the frame is left alone and refusals count. */
void     csd_ups_set_want(unsigned w);
unsigned csd_ups_get_want(void);
void     csd_ups_set_enabled(unsigned enable);   /* 'on' is a reserved word in XC */
unsigned csd_ups_is_enabled(void);

/* ---- data plane --------------------------------------------------------- */

/* 1 when the engine owns the frame (enabled AND armed AND bank loaded): the
 * DSP shelf path must stand down so exactly one stream reaches the wire. */
unsigned csd_ups_active(void);

/* One host pair IN, at host-pair cadence (UserBufferManagement). */
void     csd_ups_feed(int l, int r);

/* One wire slot OUT, at LRCLK cadence (audiohub I2S output loop). PURE POP.
 * Returns w unchanged when the path is not active. */
int      csd_ups_slot(unsigned ch, int w);

/* ---- diagnostics (read-only) -------------------------------------------- */

unsigned csd_ups_armed(void);                    /* armed regardless of grant */
unsigned csd_ups_diag_frames(void);              /* host pairs accepted this stream */
unsigned csd_ups_diag_in_hz(void);
unsigned csd_ups_diag_out_hz(void);
unsigned csd_ups_diag_refusals(void);
unsigned csd_ups_diag_rate_rejects(void);        /* non-family re-configs refused */
unsigned csd_ups_diag_pops(unsigned ch);         /* words actually delivered */
unsigned csd_ups_diag_repeats(unsigned ch);      /* empty-ring holds (starvation) */
unsigned csd_ups_diag_occ_high(unsigned ch);     /* high-water mark of the ring */
unsigned csd_ups_diag_blocks(void);              /* polyphase computes run */
unsigned csd_ups_diag_block_ticks_max(void);     /* worst burst, 10 ns ticks */
unsigned csd_ups_diag_rate_calls(void);          /* rate hand-overs accepted+seen */
unsigned csd_ups_diag_last_req_hz(void);         /* last rate requested */

#ifdef __cplusplus
}
#endif
#endif /* __csd_ups_block_h__ */
