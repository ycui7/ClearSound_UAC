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
int      csd_ups_pop_active(unsigned ch, int w); /* REVERT(dp198): no-active-check producer pop */

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
unsigned csd_ups_diag_feed_enter(void);          /* block-engine entries (11.11.30) */
unsigned csd_ups_diag_feed_pc(void);             /* last (phase<<1|chan) entered    */
unsigned csd_ups_diag_feed_in_pc(void);          /* 1 while a compute runs          */
unsigned csd_ups_diag_block_ticks_max(void);     /* worst burst, 10 ns ticks */
unsigned csd_ups_diag_push_n(void);              /* session 14 blocks-since-reset */
unsigned csd_ups_diag_push_over(void);           /* session 14 cap-backoff hits */
unsigned csd_ups_diag_rate_calls(void);          /* rate hand-overs accepted+seen */
unsigned csd_ups_diag_last_req_hz(void);         /* last rate requested */

/* ---- wire-word capture tap (bench instrumentation, 2026-11-10) ----------
 * Observes the LRCLK-cadence pops: latches the L/R pair closing each wire
 * frame at ring phase == slot (AUTO = ratio-1, mic-decimator rule) so the
 * UBM mirror in csd_dsp.xc can publish the post-resampler wire pair on the
 * mic path while the resampler is enabled. The tap advances nothing.
 * REVERT: delete these six declarations and the tap block in csd_ups_block.c
 * plus the CSD_UPS_TAP branch in csd_dsp.xc. */
void     csd_ups_tap_arm(unsigned enable);       /* follows enable lifecycle */
unsigned csd_ups_tap_armed(void);
void     csd_ups_tap_set_slot(unsigned p);       /* 0xFFFFFFFF = AUTO       */
int      csd_ups_tap_word(unsigned ch);          /* last committed pair     */
unsigned csd_ups_diag_tap_fires(void);           /* committed pairs         */
unsigned csd_ups_diag_tap_phase(void);           /* phase of last commit    */
unsigned csd_ups_diag_tap_slot_eff(void);        /* resolved slot (R-1 AUTO)*/
unsigned csd_ups_mic_owner(void);                /* 1 iff the tap owns the
                                                  * mic slots THIS frame
                                                  * (armed AND csd_ups_active
                                                  * -- never on bypassed
                                                  * ratio-1 legs) */
/* Armed-mic feed mailbox DEFINED in csd_dsp.xc (file-local to the audiohub
 * hot loop -- 11.11.7 measured that a cross-file call in UBM per wire frame
 * blows the 384 kHz budget). Producer: csd_ups_block.c (push helper + tap
 * commit, same core as the consumer). REVERT with the feed. */
extern unsigned csd_feed_owner;
extern int      csd_feed_w0;
extern int      csd_feed_w1;
extern int      csd_pop_w0;      /* 11.11.34 dual echo: pop-word mailbox */
extern int      csd_pop_w1;
extern unsigned csd_pop_seq;     /* 11.11.40 seqlock: odd = mid-commit */
/* REVERT(bp31): MIC-FRAME-ALIGNED mirror FIFO (DEFINED in csd_ups_block.c).
 * Producer = the pop side, one PAIR per host frame at ring phase 0 (a pair
 * every R pops, so the series is deterministic in the pop counter -- no
 * reader timing anywhere). Consumer = the ECHO-0 mirror in csd_dsp.xc, once
 * per host frame, at UBM time (the same instant the mic buffer for that USB
 * frame is written), selecting by monotone pair index: it advances exactly
 * one pair per host frame, so the emitted stream is a fixed decimation of
 * the pop stream and the capture's d = j - R*i is constant. Single writer,
 * single reader, same core: plain stores, no seqlock.
 *   csd_mir_prod_n  -- pairs published (monotone, never reset)            */
#define CSD_MIR_Q 64u
/* Plain objects on purpose: xcc rejects `volatile` on a reference/array
 * TYPE and cross-language symbol types must match EXACTLY (a volatile C
 * definition against this plain XC extern fails the link), so ordering is
 * enforced where it matters -- a compiler barrier in the writer between the
 * words and the count stamp. Single writer, single reader, same core. */
extern int      csd_mir_q0[CSD_MIR_Q];
extern int      csd_mir_q1[CSD_MIR_Q];
extern unsigned csd_mir_prod_n;
extern unsigned g_csd_mir_relock_n;   /* FIFO window escapes (re-locks)     */
extern unsigned g_csd_mir_lag_n;      /* live skew prod_n - cursor at a read*/
/* REVERT(bp21): mirror-selection witnesses, DEFINED in csd_dsp.xc.
 * pub_n = pairs published by the pop side (one per host frame, slot phase
 * 0), copy_n = mirror copies (one per audio frame), hold_n = copies that
 * found no new pair. Passing armed legs: delta(pub_n) == delta(copy_n) and
 * hold_n/near-zero (startup transient only). */
extern unsigned g_csd_mir_pub_n, g_csd_mir_copy_n, g_csd_mir_hold_n;
/* REVERT(armed-capture bench 2026-11-17): named witnesses. */
extern unsigned g_csd_flip_n, g_csd_tk_n;
extern unsigned g_csd_slot_n, g_csd_pub2_n;

#ifdef __cplusplus
}
#endif
#endif /* __csd_ups_block_h__ */
