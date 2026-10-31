// CSD function generator -- control surface.
//
// Scalar-only interface, per this codebase's rule that nothing crosses the
// tile boundary by pointer. The audio side lives in csd_gen.xc and is the
// ONLY translation unit that includes csd_gen_core.h; that is a memory
// requirement, not style -- the generated tables (sine 1024 + exp2/log2 257
// + Gibbs 33 words, ~20 KB) are file-scope `static const`, so a second
// includer would emit a second copy into a tile's 512 KB of reserved memory.
// Everything the SCPI/MIDI thread needs is a handful of scalars.
//
// Thread model, inherited rather than reinvented:
//
//   csd_dsp.xc already settled that the diagnostic thread and the audiohub
//   thread share memory (measured 2026-10-30: the host saw csd_dsp_bump
//   advance 1->2->3 across three preset commands) and that the safe pattern
//   is a volatile-accessed pending field plus a command counter, applied by
//   the audio thread at a frame boundary. csd_dsp_apply_pending_preset() is
//   the precedent. This module does exactly the same thing for a waveform
//   descriptor, because a 32-word struct written mid-frame by one thread and
//   read mid-frame by the other yields a descriptor whose fields come from
//   two different commands -- which sounds like a rounding bug and is not one.
//
// No floating point anywhere: XS3 has no FPU, and a soft-float call inside
// the per-sample loop costs more than the oscillator it would compute.
#ifndef _csd_gen_h
#define _csd_gen_h

#ifdef __cplusplus
extern "C" {
#endif

/* Waveform selectors -- numeric values MUST match csd_gen_mode_t in
 * csd_gen_core.h, and csd_gen.xc static-asserts every pair at build time.
 * Duplicated rather than included because the SCPI layer is C and the core is
 * included by exactly one TU (see the memory note above); a shared header of
 * numbers is the cheap half of the interface. A past duplicate here had SINE
 * and SQUARE swapped, which is not a crash -- it is a silent swap of two
 * waveforms that both look plausible. */
#define CSD_GEN_MODE_OFF       0u
#define CSD_GEN_MODE_SINE      1u
#define CSD_GEN_MODE_SQUARE    2u
#define CSD_GEN_MODE_SWEEP     3u
#define CSD_GEN_MODE_MULTITONE 4u
#define CSD_GEN_MODE_NOISE     5u

/* Sweep domain -- must match csd_gen_domain_t. */
#define CSD_GEN_DOMAIN_LIN     0u
#define CSD_GEN_DOMAIN_LOG     1u

/* _PUB suffix, not a bare name: csd_gen_core.h defines CSD_GEN_MAX_TONES too.
 * An identical redefinition is legal C, so a shared name silently compiles and
 * makes any cross-check between the two tautological -- an earlier draft of
 * the assert in csd_gen.xc compared the core's macro with itself and passed
 * regardless of what the public header said. */
#define CSD_GEN_MAX_TONES_PUB  4

/* Frequency scale: Q8, i.e. this many frequency units per Hz.
 *
 * The SCPI layer converts its integer milliHz argument with this number, and
 * GENerator:STATe? reports frequencies in this unit, so the host needs the
 * constant to interpret the reply. csd_gen.xc static-asserts it against the
 * core's CSD_GEN_HZ_SHIFT -- the same duplicate-constant discipline used for
 * the mode selectors above, because a scale that disagrees with the core
 * multiplies every frequency by 2^k rather than failing. */
#define CSD_GEN_HZ_SCALE       256u

/* Amplitude is a Q31 peak, -6 dBFS = 0x40000000. */
#define CSD_GEN_AMP_MIN        1u
#define CSD_GEN_AMP_MAX        0x7FFFFFFFu

/* Arm the generator. All frequencies are Hz in Q8 (256ths), handed as u32
 * scalars; period_ms is milliseconds and is bounded by CSD_GEN_MAX_PERIOD_MS.
 *
 * Runs on the CALLING thread (SCPI/MIDI). It only latches the request; the
 * audio thread adopts it at the next frame boundary. Returns 0, or -1 with
 * *why set to a CSD_GEN_REJ_* code, so the SCPI layer can answer with the
 * right error instead of a generic -103.
 *
 * fs_hz is a parameter rather than ambient state on purpose: a generator
 * armed for 48 kHz that the host reconfigured to 96 kHz would otherwise keep
 * its old phase increments and play a quarter-octave flat, silently. */
int csd_gen_request_arm(unsigned mode, unsigned fs_hz,
                        unsigned f_start_q8, unsigned f_stop_q8,
                        unsigned domain, unsigned amp,
                        unsigned period_ms, int *why);

/* Arm a 2..4 tone multitune. tones is the count, f_q8 up to 4 Q8 Hz values.
 * Same threading contract as csd_gen_request_arm(). */
int csd_gen_request_tones(unsigned fs_hz, unsigned tones,
                          const unsigned *f_q8, unsigned amp,
                          unsigned period_ms, int *why);

/* Stop now (one frame of silence, no ramp). */
void csd_gen_request_off(void);

/* Stream sample rate changed. Call from the audio thread's rate hook only:
 * csd_gen is not lock-protected because only that thread touches it. The
 * descriptor is kept (a 30 s log sweep means the same thing at any rate) and
 * only the frame-domain derivations are rebuilt; if the new rate makes the
 * descriptor illegal the generator goes OFF rather than play a folded wave. */
void csd_gen_notify_rate(unsigned fs_hz);

/* Host-visible state, for GENerator:STATe? and for a rig script that wants
 * proof the audio thread actually adopted the request rather than merely
 * having it parked. `adopted` advances only on the audio thread, so
 * cmd != adopted means the stream is not running. */
unsigned csd_gen_state_mode(void);
unsigned csd_gen_state_adopted(void);
unsigned csd_gen_state_cmd(void);
unsigned csd_gen_state_rejects(void);
int      csd_gen_state_last_reject(void);

/* The ADOPTED descriptor, i.e. what the audio thread is actually playing --
 * not what was last requested. A rig that trusts the request cannot tell a
 * command the audio thread never picked up from one it did, and that
 * distinction is the whole value of the cmd/adopted pair.
 *
 * Frequencies come back in Q8 Hz. They are quantised to 1/256 Hz by the
 * representation, so GENerator:STATe? reports the quantised value: a host
 * that asked for 1000.001 Hz is told 1000.0000 and does not have to discover
 * the difference by measurement. */
unsigned csd_gen_state_fs(void);
unsigned csd_gen_state_f_a_q8(void);
unsigned csd_gen_state_f_b_q8(void);
unsigned csd_gen_state_amp(void);
unsigned csd_gen_state_domain(void);
unsigned csd_gen_state_tones(void);

/* Rejection codes -- the reason a request was refused, also readable from
 * GENerator:STATe:REJect? so a mis-typed SCPI argument is diagnosable without
 * a rebuild. */
#define CSD_GEN_REJ_NONE        0
#define CSD_GEN_REJ_MODE        1   /* unknown mode selector            */
#define CSD_GEN_REJ_FS          2   /* fs_hz not a supported rate       */
#define CSD_GEN_REJ_FREQ_RANGE  3   /* f_start/f_stop outside 1 Hz..20 kHz */
#define CSD_GEN_REJ_PERIOD      4   /* period_ms zero or above the cap  */
#define CSD_GEN_REJ_AMP         5   /* amp zero or above full scale     */
#define CSD_GEN_REJ_TONES       6   /* tone count out of 2..4, or a tone
                                       outside the allowed range        */
#define CSD_GEN_REJ_DOMAIN      7   /* sweep domain not LIN or LOG      */

/* Longest output the generator will accept: 24 h. The limit is not about
 * counter width -- frame indices are u64 and the held-tail clamp already
 * makes the waveform stationary after the period, so a week would work too.
 * It is about the operator: a sweep that has run for a day is not a test
 * someone is watching, and a mistyped 1e9 ms should be an error rather than
 * a job for next month. */
#define CSD_GEN_MAX_PERIOD_MS  86400000u

/* Supported sample rates: the UAC altsettings this board advertises. A
 * generator that accepts any integer fs will happily arm at 44100.12 and
 * produce a waveform whose frequency is wrong by a rounding nobody
 * investigates. */
int csd_gen_fs_supported(unsigned fs_hz);

/* ---- called by csd_dsp.xc only ---------------------------------------- */

/* Adopt any latched request at a frame boundary. Idempotent and cheap when
 * nothing is pending (one volatile load and a compare). */
void csd_gen_apply_pending(void);

/* Fill the frame with the active waveform. `out` is a 2-sample (L,R) frame in
 * the host's int32 representation, same convention as the rest of csd_dsp:
 * value 2^31 is full scale. Called once per frame by UserBufferManagement
 * after the preset is adopted and before the diagnostics.
 *
 * Inlining matters: at 48 kHz/192 kHz this runs every 21 us and the per-term
 * cost of the square is a dozen XS3 cycles, so a call per frame is affordable
 * but a call per sample is not. The parameter is `samp`, not `out`: `out` is a
 * keyword in XC and the compiler rejects it as a parameter name. */
void csd_gen_fill(int samp[2]);

/* Firmware-facing wrapper: write the wave into a USB->DAC frame, or touch
 * nothing.
 *
 * Returns 1 if it wrote the frame, 0 if it left the buffer alone. The check
 * lives HERE rather than at the call site on purpose: csd_gen_fill() writes
 * zeros when the mode is OFF, so a call site that called it unconditionally
 * would silence all audio the moment the generator was built in -- a
 * regression that every acoustic test would then fail loudly for, but only
 * after someone enabled the feature. The call site cannot get it wrong, and
 * OFF is byte-exact pass-through by construction rather than by convention.
 *
 * `s` is the same buffer the DSP engine edits in place: s[0] left, s[1]
 * right, int32-as-unsigned, 2^31 full scale.
 */
int csd_gen_inject(unsigned *s);

#ifdef __cplusplus
}
#endif
#endif /* _csd_gen_h */
