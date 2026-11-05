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
#define CSD_GEN_MODE_DC        6u   /* constant offset = amp, both channels */

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

/* Per-frame gate accounting (audio thread; deltas are the rig's evidence).
 * csd_gen_note is called by the frame hook ONLY while armed: 0 emitted,
 * 1 suppressed by DSD/native stream mode, 2 suppressed by the DoP latch. */
void     csd_gen_note(unsigned why);
unsigned csd_gen_cnt_emit(void);
unsigned csd_gen_cnt_sup_dsd(void);
unsigned csd_gen_cnt_sup_dop(void);
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

/* ---- pre-mixer injection (mixer builds only) ---------------------------
 *
 * When the USB mixer is compiled in, the wave is SUMMED into the host stream
 * BEFORE the mixer so a test tone rides the same gain, routing and mapping
 * path as real audio (user requirement: "generator before the mixer, so
 * function can be summed to the host audio stream for testing purposes").
 *
 * Where the generator injects is decided from the same -DMIXER that builds
 * the mixer (xua_conf_default.h:1332 maps MIXER -> XUA_MIXER_EN). Derived,
 * not configured: if the two could be set independently, both the mixer
 * thread and the audio thread would advance the NCO and every tone would
 * measure at exactly twice its commanded frequency. mixer.xc repeats this
 * condition literally -- it cannot include this header, being library code.
 */
#if defined(CSD_GEN_EN) && defined(MIXER) && (MIXER)
#define CSD_GEN_PREMIX 1
#endif

/* DoP marker test on one 32-bit channel word: DoP16-in-S32LE carries the
 * marker in bytes 0 and 2, alternating 0x05 / 0xFA per frame. Single
 * definition: csd_dsp.xc (post-mixer detector) and csd_gen.xc (pre-mixer
 * detector) must agree exactly or the two latches disagree about what DoP
 * is. The probability PCM fools it is ~2^-16 per frame, which is why
 * detection votes (4 in, 8 out) rather than trusting one frame. */
#define CSD_GEN_DOP_MARK(s) ( ((s) & 0xffu) == (((s) >> 16) & 0xffu) && \
                              (((s) & 0xffu) == 0x05u || ((s) & 0xffu) == 0xFAu) )

/* ONE call per frame from the mixer thread at the frame boundary, then one
 * scalar query per output channel. Deliberately scalar in, scalar out -- no
 * array or pointer crosses into lib_xua, because mixer.xc lives in the
 * library and an array parameter is a pointer in disguise: correct today by
 * accident of tile placement, silently wrong the day the mixer moves. The
 * audiohub hook takes scalars for the same reason.
 *
 * begin() owns the frame boundary: adopt any pending command, pick up a
 * stream-start rate the audio thread left behind, vote the DoP signature on
 * the RAW host pair (before the sum can mask the markers) and advance the NCO
 * exactly once. Doing the advance here rather than in the per-channel query
 * is what guarantees one step per frame -- advancing per channel would
 * double the frequency, and would be invisible to anything but a rig built to
 * catch exactly that.
 *
 * The tone is summed into the I2S pair (channels 0 and 1) only. Other host
 * channels (SPDIF/ADAT) ask, get their own sample back untouched, and so
 * cannot sprout a tone nobody asked for.
 *
 * `dsd_mode` is lib_xua's own value at AUDIO_START: 0 off, 1 DoP, 2 native
 * (xua_audiohub.xc:586-588). Any non-zero value shuts the sum off.
 */
#ifdef CSD_GEN_PREMIX
void csd_gen_mix_begin(int hostL, int hostR, unsigned dsd_mode);
int  csd_gen_mix_sample(unsigned ch, int host);   /* pre-volume path, MAX_MIX_COUNT>0 builds only */
int  csd_gen_mix_host_only(unsigned ch, int host);   /* spec 2026-11-04: w_host on USB audio only */
int  csd_gen_mix_post_volume(unsigned ch, int volled_host); /* + FG, no FG volume, mixer1 only */
#endif

/* Post-mixer bypass input. The mixer thread owns DoP detection in a mixer
 * build (it is the only thread that sees the host frame before the tone is
 * summed in), so the audio thread must read its verdict rather than rely
 * solely on its own detector, whose input is already tone + host.
 * Single word, single writer: atomic read on XS3. */
#ifdef CSD_GEN_PREMIX
unsigned csd_gen_mix_dop(void);
#endif

/* Stream-start hand-off from the audio thread (csd_dsp_set_rate runs there)
 * to the mixer thread, which owns csd_gen in a mixer build. Leaves a single
 * word; the owner collects it at its next frame boundary and calls
 * csd_gen_notify_rate() itself.
 *
 * The two cross-thread words live in csd_gen_mailbox.c, a C translation
 * unit, NOT as statics here. That is not style: lib_xua puts mixer1 and
 * XUA_AudioHub in the same par block, and XC's parallel-usage checker
 * refuses to link when two branches of one par touch the same variable --
 * `unsafe` does not exempt it, because the checker attributes the reference
 * at the `&` in the caller rather than the deref inside the helper. Measured:
 * "use of `usage.anon.37' violates parallel usage rules". lib_xua uses C TUs
 * for its own cross-boundary hooks (xua_buffman_default.c) for the same
 * reason. Each word still has exactly one writer and one reader, and is a
 * single aligned unsigned, so a read cannot tear -- see that file for the
 * discipline, which nothing now enforces automatically. */
#ifdef CSD_GEN_PREMIX
void csd_gen_mix_set_rate(unsigned fs_hz);
#endif

/* ---- cross-thread mailbox (defined in csd_gen_mailbox.c, a C TU) --------
 *
 * One word each, one writer and one reader per word, never both read and
 * written from the same thread. Deliberately NOT exposed as shared statics:
 * see the note above on XC's parallel-usage checker. A caller that needs two
 * words to agree must not read two of these -- that needs a channel. */
void     csd_mb_put_rate(unsigned fs_hz);   /* writer: audio thread   */
unsigned csd_mb_get_rate(void);             /* reader: mixer thread   */
void     csd_mb_put_dop(unsigned dop);      /* writer: mixer thread   */
unsigned csd_mb_get_dop(void);              /* reader: audio thread   */

/* Mixer volume-command trace (see csd_gen_mailbox.c): writer is the mixer
 * thread's SET_MIX_OUT_VOL handler, reader is the diagnostic thread. */
void     csd_mb_note_vol(unsigned index, unsigned val);
unsigned csd_mb_get_vol_cmds(void);
unsigned csd_mb_get_vol_index(void);
unsigned csd_mb_get_vol_val(void);

/* DoP vote probe (mixer builds; writer is the mixer thread inside
 * csd_gen_mix_begin, reader is the diagnostic thread). Tells whether the
 * words the mixer votes on still carry DoP markers, which is what the
 * 27 509-frame late latch on the mixer artifact needs attributed. Reads are
 * independent monotones, never a coherent snapshot. */
/* Per-source gains (Q15, 32768 = unity), packed (w_gen<<16)|w_host in one
 * word so both move together. Writer: diagnostic thread. Reader: mixer
 * thread at its frame boundary. See csd_gen_mailbox.c. */
#define CSD_GEN_Q15_UNITY 32768u
void     csd_mb_put_gains(unsigned packed);
unsigned csd_mb_get_gains(void);

/* L/R trim pair (TODO items 3+4): packed (trimR<<16)|trimL, int16 each in
 * 8.8 dB, 0..-8192 (0 dB .. -32 dB), 32-count (0.125 dB) steps. See
 * csd_gen_mailbox.c for the single-word rationale. Production, not debug:
 * this is manufacturing calibration, so it survives the release gate. */
void     csd_mb_put_trim(unsigned packed);   /* posts trim-dirty too     */
unsigned csd_mb_get_trim_dirty(void);        /* boot/config-pass refresh */
void     csd_mb_clear_trim_dirty(void);
unsigned csd_trim_master_trim_q31(unsigned packed);  /* the ONE conversion */
/* MASTER TRIM base register (ES9219Q regs 17..20 == 0x11..0x14). Named
 * here rather than pulling es9219q.h across the mailbox boundary. */
#define CSD_TRIM_REG_BASE 0x11u
/* Byte order of the 32-bit MASTER TRIM across regs 17..20: SETTLED by
 * measurement on 2026-11-05 -- reg 0x11 holds the LEAST significant byte.
 * Datasheet v1.3 gives value semantics (signed, full scale 32'h7FFFFFFF,
 * -1 dB = 0x721482BF, which evaluates to exactly -1.000 dB as Q31) but no
 * bit-to-register map; the chip's convention comes from the adjacent volume
 * pair ([7:0] VOLUME_CH1 = reg 15, [15:8] VOLUME_CH2 = reg 16) and from this
 * project's own ES9219Q_VOL_CTRL_LO/HI names. Acoustic ladder in
 * csd-js-tester/rig/trim_boot_gate.py: unity as FF FF FF 7F is audible and
 * monotonic over 0..-30 dB; the same word as 7F FF FF FF is -101.8 dBFS at
 * the 1 kHz bin (74 dB down = inaudible). The former MSB-first default was a
 * header guess and it muted the product; -DCSD_TRIM_BYTES_MSB_FIRST exists
 * only to reproduce that build. Do not re-reason this from the header. */
/* Byte idx (0 = reg 17) of the Q31 word, order behind the ifdef above.
 * DEFINED IN csd_gen_mailbox.c (a plain function, not a header inline: no
 * static-inline precedent in this codebase and XC's parser has rejected
 * unexpected forms before -- see csd_diag_regs.xc's parser notes). */
unsigned csd_trim_reg_byte(unsigned q31, unsigned idx);
unsigned csd_mb_get_trim(void);

/* L/R trim as a mixer output multiplier (TODO items 3+4, 2026-11-01).
 * Returns the Q2.29 multiplier the mixer must apply to channel ch in
 * addition to the host's own multOut[] value: 0x20000000 = 0 dB (the
 * library's MAX_VOLUME_MULT), and the 0.125 dB grid down to -32 dB is
 * representable here (it is NOT in the ES9219Q digital volume, which is
 * 0.5 dB/LSB -- the reason trim lives in the mixer and not the codec).
 * Recomputed only when the trim word changes, never per sample: XS3 has
 * no FPU, so a per-frame log/exp would be a real cost on the tile that
 * also runs the hard-real-time audio handshake. Callers: mixer.xc. */
unsigned csd_mix_apply_trim(unsigned ch, unsigned host_mult);

void csd_mb_note_dopvote(unsigned match, unsigned l, unsigned r);

/* Mixer-frame proof-of-life: bump from mixer1's sample loop; read from the
 * diagnostic thread. See csd_gen_mailbox.c for why it exists. */
void     csd_mb_note_mix_frame(void);
unsigned csd_mb_get_mix_frames(void);
/* Peak ticks mixer1 held the audio() thread between the request token and
 * the sample exchange (see csd_mb_note_mix_busy). 12500 ticks = one 48 kHz
 * sample period. Clear with MIXer:DACPeak:CLEAR. */
unsigned csd_mb_get_mix_busy_peak(void);
/* Peak ticks mixer1 spent after unblocking audio(), before its next request
 * (generator hand-off + DoP vote). Deadlines need busy + tail together. */
void     csd_mb_note_mix_tail(unsigned delta_ticks);
unsigned csd_mb_get_mix_tail_peak(void);
/* Proof that the chip bring-up actually ran: csd_AudioHwConfig increments on
 * the audiohub core. A rig leg that claims to have forced a rate change must
 * show this counter ADVANCE, or its conclusion is void (plughw resamples). */
void     csd_mb_note_hwcfg(unsigned sam_freq, unsigned mclk);
void     csd_mb_get_hwcfg(unsigned *count, unsigned *freq, unsigned *mclk);
/* Node ids for the peak instrument (csd_mb_note_dac_sample). Shared because
 * nodes 0..4 are written by lib_xua's mixer1 and 5..6 by the audio thread's
 * UserBufferManagement. Keep in sync with CSD_DACNODES in the mailbox. */
#define CSD_NODE_HOST    0u   /* received from host (GetSamplesFromHost)      */
#define CSD_NODE_POSTMIX 1u   /* read from ptr_samples post-mix/pre-volume    */
#define CSD_NODE_VOLUME  2u   /* after the output-volume MAC                  */
#define CSD_NODE_DAC     3u   /* the word mixer1 handed to the audio thread   */
#define CSD_NODE_TOKEN   4u   /* trigger token from the audio thread          */
#define CSD_NODE_PREDSP  5u   /* audio() received, before csd_dsp_frame       */
#define CSD_NODE_POSTDSP 6u   /* after csd_dsp_frame: the I2S-bound words     */

void     csd_mb_note_dac_sample(unsigned ch, unsigned node, int sample);
void     csd_mb_note_host_nonzero(unsigned ch, int sample, unsigned frame);
void     csd_mb_get_host_nonzero(unsigned ch, unsigned *count,
                                 unsigned *first_word);
unsigned csd_mb_get_host_first_frame(void);
void csd_mb_note_i2s_delta(unsigned short delta_ticks);

/* ---- DAC->ADC loopback tap (bit-perfect validation, CSD_LOOPBACK_TAP) ----
 *
 * WHAT IT IS: when armed, the audiohub loop substitutes the words it was
 * about to hand to the I2S DAC port into the ADC slots that the USB IN
 * endpoint streams to the host. The host therefore reads back, on the
 * capture stream, the exact words its own playback stream reached at the
 * I2S boundary -- and can compare them for an hour.
 *
 * WHAT THAT COVERS, precisely: USB OUT -> endpoint buffers -> feedback/
 * resample -> mixer -> DSP -> samplesOut -> [TAP] -> [INJECT] -> samplesIn
 * -> USB IN. i.e. every step of "XMOS internal processing", which is the
 * question asked. Dropped, duplicated, reordered or zero-substituted frames
 * are all visible, because the stimulus carries its own frame index.
 *
 * WHAT IT DOES NOT COVER, stated so nobody over-claims it: the tap sits
 * BEFORE the port's bit-reversal and shift-out and the injection sits
 * AFTER the ADC port's shift-in, so the serialiser, the pin, the 39 ohm
 * series resistor and the trace are outside the loop. A 1-bit slip in the
 * port config or a clock-phase fault passes this tap clean. Those need the
 * word observed on the wire: an external jumper into a spare input (same
 * host rig, different source) or a logic analyser.
 *
 * THREADING: csd_mb_loop_en and csd_mb_loop_tx[] are read and written only
 * by tasks on the audio IO tile (tile[1]): XUA_AudioHub runs the loop, and
 * the SCPI/diagnostic task is also tile[1] (csd_diag_midi.xc's own comment
 * says so). These are C file-scope statics in a per-tile image, i.e. tile-
 * LOCAL: a tile[0] caller would silently read 0 and appear healthy. That is
 * the exact shape of the trim persistence bug, so any future tile[0] caller
 * must move the state to a channel instead of trusting this.
 *
 * COST: one volatile load + one branch per channel per LRCLK frame, and
 * only when CSD_LOOPBACK_TAP is compiled in. Off by default, and the whole
 * feature compiles away when the flag is absent -- nothing in the per-frame
 * path of a shipping image.
 */
void csd_mb_loop_put(unsigned ch, unsigned word);   /* writer: audiohub TX */

/* Reader, audiohub RX. Returns the tapped word when armed, or `fallback`
 * (the sample the port actually received) when not -- scalar in, scalar out,
 * because a C function taking unsigned* does not even link when called from
 * XC, and a sentinel value cannot be used: 0 is a legitimate sample and the
 * rig must be able to see a zero-substituted underrun, not have one handed
 * to it as "disarmed". */
unsigned csd_mb_loop_take(unsigned ch, unsigned fallback);

void csd_mb_loop_set_enable(unsigned enable);       /* writer: SCPI task    */
void csd_mb_loop_get(unsigned *en, unsigned *frames);

/* Tap source revision, reported by DEBUG:LOOPback:INDEX? so a rig can prove
 * which tap it measured -- a stale .xe answering with the OLD row set is
 * otherwise indistinguishable from a fresh one. Bump on every change to the
 * tap's put()/take() indexing. */
/* 5 = hot-path histograms moved behind CSD_LOOPBACK_TAP_VERBOSE, so an armed
 * tap now costs one volatile store + one increment per channel per half-frame
 * instead of ~5 RMWs, and a DISARMED tap costs a single volatile load (it
 * previously paid the histogram RMWs even when off). */
#define CSD_LOOPBACK_TAP_BUILD  5u

/* Define (via -D) to re-enable the per-site / per-channel arrival histograms.
 * They cost volatile RMWs inside the audio() task's 781-tick half-frame budget
 * at 384 kHz, so they are OFF unless asked for: diagnose with them, then rebuild
 * without them before measuring rate again. */
/*#define CSD_LOOPBACK_TAP_VERBOSE 1*/

/* Per-site, per-channel arrival counters for the tap's put()/take(). Lets the
 * rig read, from the device, which channel index each site presents -- the
 * source of the two TX sites reads frameCount+i and looks correct, yet on
 * hardware both capture channels returned host R's word, so source review was
 * not sufficient. site 0 = first TX half-frame, site 1 = second TX half-frame. */
void csd_mb_loop_put_site(unsigned site, unsigned ch, unsigned word);
void csd_mb_loop_get_index(unsigned *put_ch, unsigned *take_ch,
                           unsigned *site0, unsigned *site1);
/* The WORDS, not the counters: site_word[0..1] = what each TX site last saw
 * from samplesOut, tx[0..1] = what the tap serves to the two RX slots. Index
 * counters were not sufficient -- the fault is a value fault, and every device
 * writer reads correctly indexed in source. See csd_gen_mailbox.c. */
void csd_mb_loop_get_words(unsigned *site_word, unsigned *tx);
void csd_mb_get_i2s(unsigned *frames, unsigned *bad, unsigned *hi,
                    unsigned *lo, unsigned *first_bad);
void csd_mb_clear_i2s(void);
void     csd_mb_clear_dac_peak(void);
unsigned csd_mb_get_dac_peak(unsigned ch, unsigned node);
unsigned csd_mb_get_dac_peak_word(unsigned ch, unsigned node);
void csd_mb_get_dopvote(unsigned *frames, unsigned *match,
                        unsigned *lastL, unsigned *lastR);

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
