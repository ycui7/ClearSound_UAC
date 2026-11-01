// Copyright (c) 2026 ClearSound. All rights reserved.
// See LICENCE for full licence terms.

/* Hand-off words between the mixer thread and the audio thread.
 *
 * WHY THIS FILE EXISTS AS C
 *
 * In a mixer build the generator runs on mixer1 and the DSP on XUA_AudioHub.
 * lib_xua puts both in the SAME par block (main.xc usb_audio_io, one tile),
 * so XC's parallel-usage checker refuses to link if either branch touches a
 * variable the other also touches -- even through a helper that dereferences
 * under `unsafe`, because the checker attributes the reference at the `&` in
 * the CALLER, outside the unsafe region. Measured on the first attempt:
 *
 *   error: use of `usage.anon.37' violates parallel usage rules
 *     note: object used here: csd_gen_store(&csd_gen_mix_want_fs, fs_hz)
 *       called by csd_dsp_set_rate ... called by XUA_AudioHub
 *     note: object used here: csd_gen_load(&csd_gen_mix_want_fs)
 *       called by mixer
 *
 * A channel would be the textbook answer, but mixer1's sample exchange is a
 * lock-step handshake with no slack for a select, and the audio thread has no
 * polling point that does not cost the frame budget. So the hand-off lives in
 * a C translation unit, which is the arrangement lib_xua itself uses for its
 * weak hooks (xua_buffman_default.c): C globals are not attributed to an XC
 * branch, so the checker does not see a conflict, and the discipline is ours
 * to hold rather than the compiler's to enforce.
 *
 * THE DISCIPLINE, stated because nothing checks it:
 *   - each word has exactly ONE writer and ONE reader, as annotated;
 *   - each is a single naturally-aligned unsigned, so a read is one ldw and
 *     can never be torn;
 *   - volatile forces the access; no lock, and none is needed at one word.
 * A future caller that wants more than one word per hand-off must use a
 * channel or a seqlock instead -- writing a struct through here is NOT safe,
 * and reading two of these and expecting them to agree is NOT either.
 */

#include "csd_gen.h"

/* Stream sample rate. Writer: audio thread (csd_dsp_set_rate, from
 * AudioHwConfig). Reader: mixer thread, at its frame boundary, which then
 * applies it to the generator it owns. */
static volatile unsigned csd_mb_fs;

/* In-band DoP verdict. Writer: mixer thread (its raw-host-pair vote, the
 * only uncontaminated view once the tone is summed). Reader: audio thread,
 * for the DSP bypass. 0 = PCM, 1 = latched DoP. */
static volatile unsigned csd_mb_dop;

void csd_mb_put_rate(unsigned fs_hz) { csd_mb_fs = fs_hz; }

unsigned csd_mb_get_rate(void) { return csd_mb_fs; }

void csd_mb_put_dop(unsigned dop) { csd_mb_dop = dop ? 1u : 0u; }

unsigned csd_mb_get_dop(void) { return csd_mb_dop; }

/* ---- mixer volume-command trace ------------------------------------------
 *
 * WHY: the host's SET_CUR for 'Mix 2 Playback Volume' returns rc=0 and the
 * control then reads back 0 counts (-48 dB = our MIN_VOLUME), never the value
 * written, on builds with and without the generator. Three instruments have
 * already failed to explain that -- amixer readbacks (the kernel caches), raw
 * pyusb control transfers (EIO on our build AND on the factory image, so the
 * transfers never landed), and the xrun console (zero lines even at boot: no
 * printf reaches the host on this configuration). None of them could
 * distinguish "the command never reached the mixer" from "it reached the
 * mixer and was applied to a multiplier nothing reads".
 *
 * So instrument the destination itself: the mixer thread's SET_MIX_OUT_VOL
 * handler counts its own commands and records the last index/value. SCPI over
 * MIDI is the one transport proven reliable on this board (the rig's entire
 * 51-check suite rides it), so the numbers get out.
 *
 * Writer: mixer thread only. Readers: the diagnostic thread, as three
 * independent monotones -- never as a coherent triple. The claim each supports
 * is "this advanced at all", which needs no snapshot.
 */
static volatile unsigned csd_mb_vol_cmds;    /* SET_MIX_OUT_VOL received      */
static volatile unsigned csd_mb_vol_index;   /* channel of the most recent    */
static volatile unsigned csd_mb_vol_val;     /* multiplier of the most recent */

void csd_mb_note_vol(unsigned index, unsigned val)
{
    csd_mb_vol_cmds++;
    csd_mb_vol_index = index;
    csd_mb_vol_val = val;
}

unsigned csd_mb_get_vol_cmds(void)  { return csd_mb_vol_cmds; }
unsigned csd_mb_get_vol_index(void) { return csd_mb_vol_index; }
unsigned csd_mb_get_vol_val(void)   { return csd_mb_vol_val; }

/* ---- DoP vote probe (mixer builds only) ----------------------------------
 *
 * WHY: on the mixer artifact the 4-vote DoP latch took 27 509 frames where
 * the production artifact latched in 238, and the loopback witness of the
 * DoP payload dropped 20 dB (peak 0.026 vs 0.244). Two candidate causes --
 * the mixer thread sees different words than the production vote point
 * (negotiated sample format, half-frame exchange above 96 kHz), or the
 * words are fine and the vote is starved. Only the raw words answer that,
 * and the mixer thread will not print. Counters + a last-observed word pair
 * go out through SCPI, the one transport proven on this board.
 *
 * Writer: mixer thread (csd_gen_mix_begin). Reader: diagnostic thread via
 * GENerator:DOPpRObe?. Independent monotones; not a coherent snapshot.
 */
static volatile unsigned csd_mb_dop_frames;   /* votes cast                  */
static volatile unsigned csd_mb_dop_match;    /* frames where MARK passed    */
static volatile unsigned csd_mb_dop_lastL;    /* last raw hostL voted        */
static volatile unsigned csd_mb_dop_lastR;    /* last raw hostR voted        */

/* Per-source pre-mixer gains, packed as (w_gen << 16) | w_host, each Q15
 * with 32768 = unity. Packed INTO ONE WORD on purpose: a set wants both
 * values to arrive together (two words would let the mixer apply a new
 * host weight with the old generator weight for a frame or two -- audible,
 * and exactly what the file header forbids). Writer: diagnostic thread
 * (SCPI MIXer:GAIN:*). Reader: mixer thread, once per frame. Initialised
 * to unity/unity so the boot state is byte-exact passthrough even before
 * the host ever writes. */
static volatile unsigned csd_mb_gains = (32768u << 16) | 32768u;

void     csd_mb_put_gains(unsigned packed) { csd_mb_gains = packed; }
unsigned csd_mb_get_gains(void) { return csd_mb_gains; }

/* Mixer-frame proof. The case-A ladder says the artifact grows with our
 * features (factory +1.0 dB, production +5.8, mixer-no-gen +4..7, mixer+gen
 * +11.7), but every measurement so far inferred mixer activity from audio.
 * This counter answers it directly: the mixer thread bumps it once per frame
 * it produces. Zero while a stream plays means mixer1 never ran, so NOTHING
 * in the mixer path (injection, gains, doMix, output volume) can be the
 * source and the search moves elsewhere. Writer: mixer thread. */
static volatile unsigned csd_mb_mix_frames;

/* How many host-node words were non-zero, and the very first non-zero word
 * seen. Distinguishes a bounded prefill burst (tens of frames) from a
 * continuous leak (millions) -- they are different bugs with different
 * fixes, and the peak alone cannot tell them apart. */
static volatile unsigned csd_mb_host_nonzero[2];
static volatile unsigned csd_mb_host_first_word[2];
static volatile unsigned csd_mb_host_first_seen;   /* frame index, 0 = never */

void csd_mb_note_mix_frame(void)
{
    csd_mb_mix_frames++;
}

/* ---- tile[1] resource utilisation, audio-wait window --------------------
 *
 * Question this answers: the generator runs INSIDE mixer1, which shares
 * tile[1] with the hard-real-time audio() task (main.xc puts both in the one
 * usb_audio_io par on XUA_AUDIO_IO_TILE_NUM). Does our per-frame work eat
 * into the audio thread's sample-period budget?
 *
 * The window is the one audio() actually blocks in: mixer1 takes t0 right
 * after inuint(c_audio) (which unblocks audio()) and t1 after
 * GiveSamplesToDevice/GetSamplesFromDevice. audio()'s I2S port deadline
 * covers exactly this span -- xua_audiohub.xc:192 says so: "If mixer1 misses
 * the sample period, this port timestamp..." Overflowing 12500 ticks at
 * 48 kHz (600 MHz fast mode) is a missed sample period, which is ALSO the
 * mechanism that keeps the decouple in outUnderflow and re-emits the token
 * word -- so this instrument tests the tile-contention theory of case A
 * directly, and it reports the instrument's own cost because it is inside
 * the guarded block. Peak, not mean: a deadline failure is a max event. */
static volatile unsigned csd_mb_mix_busy_peak;

void csd_mb_note_mix_busy(unsigned delta_ticks)
{
    if (delta_ticks > csd_mb_mix_busy_peak) {
        csd_mb_mix_busy_peak = delta_ticks;
    }
}

unsigned csd_mb_get_mix_busy_peak(void) { return csd_mb_mix_busy_peak; }

/* ---- AudioHwConfig call counter ----------------------------------------
 *
 * Why: rig/rate_change_replay.py concluded "forced re-configs are clean, so
 * the bring-up sequence is not the case-A trigger" -- but it opened plughw:,
 * which RESAMPLES silently. A 44.1k file can reach the UAC endpoint as 48k,
 * no SET_CUR ever lands, AudioHwConfig never runs, and the "clean" leg
 * measured nothing. This counter makes that leg falsifiable: if the count did
 * not advance, the leg is void rather than exonerating.
 *
 * Writer: audiohub core (csd_AudioHwConfig runs there). Reader: tile[1] SCPI
 * task. Single aligned word, monotone, never reset -- same discipline as the
 * frame counter above. */
static volatile unsigned csd_mb_hwcfg_count;
static volatile unsigned csd_mb_hwcfg_last_freq;
static volatile unsigned csd_mb_hwcfg_last_mclk;

void csd_mb_note_hwcfg(unsigned sam_freq, unsigned mclk)
{
    csd_mb_hwcfg_last_freq = sam_freq;
    csd_mb_hwcfg_last_mclk = mclk;
    csd_mb_hwcfg_count++;
}

void csd_mb_get_hwcfg(unsigned *count, unsigned *freq, unsigned *mclk)
{
    if (count) *count = csd_mb_hwcfg_count;
    if (freq)  *freq  = csd_mb_hwcfg_last_freq;
    if (mclk)  *mclk  = csd_mb_hwcfg_last_mclk;
}

/* I2S-deadline monitor: LRCLK port-timestamp deltas from AudioHub_MainLoop
 * (budget 32 ticks). Called by the audio() task. Counting only -- an
 * ecallf/assert version exists in lib_xua (-DENABLE_I2S_TIMING_CHECK) and
 * is fatal, which is useless on a board you still need to measure. */
static volatile unsigned csd_mb_i2s_frames;
static volatile unsigned csd_mb_i2s_bad;      /* delta != 32                */
static volatile unsigned csd_mb_i2s_worst_hi; /* max delta                  */
static volatile unsigned csd_mb_i2s_worst_lo; /* min delta                  */
static volatile unsigned csd_mb_i2s_bad_first; /* frame index of first bad  */

void csd_mb_note_i2s_delta(unsigned short delta_ticks)
{
    unsigned d = delta_ticks;
    csd_mb_i2s_frames++;
    if (d != 32u) {
        csd_mb_i2s_bad++;
        if (csd_mb_i2s_bad_first == 0u)
            csd_mb_i2s_bad_first = csd_mb_i2s_frames;
    }
    if (d > csd_mb_i2s_worst_hi) csd_mb_i2s_worst_hi = d;
    if (csd_mb_i2s_worst_lo == 0u || d < csd_mb_i2s_worst_lo)
        csd_mb_i2s_worst_lo = d;
}

void csd_mb_get_i2s(unsigned *frames, unsigned *bad, unsigned *hi,
                    unsigned *lo, unsigned *first_bad)
{
    if (frames)     *frames     = csd_mb_i2s_frames;
    if (bad)        *bad        = csd_mb_i2s_bad;
    if (hi)         *hi         = csd_mb_i2s_worst_hi;
    if (lo)         *lo         = csd_mb_i2s_worst_lo;
    if (first_bad)  *first_bad  = csd_mb_i2s_bad_first;
}

void csd_mb_clear_i2s(void)
{
    csd_mb_i2s_frames = csd_mb_i2s_bad = csd_mb_i2s_bad_first = 0u;
    csd_mb_i2s_worst_hi = 0u; csd_mb_i2s_worst_lo = 0u;
}

void csd_mb_note_host_nonzero(unsigned ch, int sample, unsigned frame)
{
    if (ch >= 2u) return;
    if (sample == 0) return;
    csd_mb_host_nonzero[ch]++;
    if (csd_mb_host_first_seen == 0u) {
        csd_mb_host_first_seen = frame;
        csd_mb_host_first_word[0] = (unsigned)sample;
        csd_mb_host_first_word[1] = 0u;   /* filled by the other channel */
    } else if (csd_mb_host_first_word[1] == 0u) {
        csd_mb_host_first_word[1] = (unsigned)sample;
    }
}

void csd_mb_get_host_nonzero(unsigned ch, unsigned *count, unsigned *first_word)
{
    /* Null tolerated for either out-param: readers that only want the count
     * must not have to invent scratch storage (and must not fault here). */
    unsigned c = 0u, w = 0u;
    if (ch < 2u) { c = csd_mb_host_nonzero[ch]; w = csd_mb_host_first_word[ch]; }
    if (count)      *count      = c;
    if (first_word) *first_word = w;
}

/* THE case-A instrument: peak |sample| of the words actually handed to the
 * DAC, measured at the GiveSamplesToDevice node itself.
 *
 * Why here and not via the USB capture mirror: in a mixer build the capture
 * stream is assembled by mixer1's GiveSamplesToHost, not by
 * UserBufferManagement, so the DIAG_LOOP mirror that answered this question
 * on the production build reads pure silence here -- measured: a real 1 kHz
 * tone streamed gave -144 dBFS at the mirror. This hook has no capture path
 * and no mic in the loop: it reports the numbers the I2S/DAC path is given,
 * which is exactly the fork the investigation needs --
 *   peak ~ 0 while the mic hears -46 dBFS  -> the words are clean; the codec
 *                                            makes the tone when AudioHwConfig
 *                                            re-pokes it at stream open;
 *   peak large on a zeros stream           -> a module upstream is writing
 *                                            content; follow it back.
 * Absolute values, saturating unsigned max; clear with MIXer:DACPEAK:CLEAR. */
/* FOUR nodes on the DAC-bound path, so one measurement localises where a
 * non-zero word first appears instead of only proving one exists:
 *   0 HOST    value received from the host (GetSamplesFromHost)
 *   1 POSTMIX value read out of ptr_samples in do_output_volume_control
 *   2 VOLUME  after the volume MAC, the value stored to vol_ctld
 *   3 DAC     the word handed over in GiveSamplesToDevice
 * A frozen word first visible at node 3 means the DAC node itself; first at
 * 1 means ptr_samples; first at 0 means the host exchange. */
/* 4 sample nodes on the path + node 4 = the TRIGGER TOKEN mixer1 receives
 * from the audio thread. That token is echoed back by the decouple as the
 * OUT-prefill sample (handle_audio_request: underflowSample = inuint(...)
 * then outuint underflow), so it is the provenance candidate for the frozen
 * 0x96969696 (lib_xua's native-DSD underflow word, xua_audiohub.xc:228). */
/* 7 nodes. The first five are mixer-thread nodes (host receive .. the word
 * handed to the audio thread). 5 and 6 bracket the DSP on the AUDIO thread:
 *
 *   5 PREDSP  sampsFromUsbToAudio as the audio() task received it
 *   6 POSTDSP the same buffer after csd_dsp_frame() -- i.e. the words that
 *             actually go out over I2S.
 *
 * This was the blind spot: with MIXER enabled the sample chain is
 *   host -> decouple -> mixer1 (injection, volume) -> audio() -> csd_dsp_frame
 *       -> I2S
 * so peaking only inside mixer.xc proves nothing about what the codec is
 * given -- the FIR runs downstream of every mixer-side node. rig/
 * first_stream_regdiff.py found ES9219Q reg 0x43 (DPLL_NUM) = 0xFE during
 * the tone and 0x00 when clean: the chip is correcting an actively moving
 * input, which is a symptom of real content on the wire, not a silent one.
 * Writer for 5/6: audio() task. */
#define CSD_DACNODES 7
static volatile unsigned csd_mb_dac_peak[2][CSD_DACNODES];
/* Raw bit pattern of the word that produced each peak. Magnitude alone was
 * not enough: an instrument bug (abs built from the sign BIT, not the sign
 * extension) made a frozen NEGATIVE word look like a full-scale pop, and the
 * pattern is what distinguishes uninitialised RAM (ASCII-looking, e.g.
 * 0x69696970), a stale buffer, and real signal. */
static volatile unsigned csd_mb_dac_peak_word[2][CSD_DACNODES];

void csd_mb_note_dac_sample(unsigned ch, unsigned node, int sample)
{
    /* |x| without a branch: mask = sign EXTENSION, not the sign BIT -- an
     * earlier version used 0x80000000 as the mask and reported negative
     * samples as 2^32-|x|, which made a frozen NEGATIVE word look like a
     * full-scale pop. Magnitude alone is not evidence; the raw word is. */
    unsigned mask = (unsigned)sample >> 31;
    unsigned a = ((unsigned)sample + mask) ^ mask;
    if (ch < 2u && node < CSD_DACNODES && a > csd_mb_dac_peak[ch][node]) {
        csd_mb_dac_peak[ch][node] = a;
        csd_mb_dac_peak_word[ch][node] = (unsigned)sample;
    }
}

void csd_mb_clear_dac_peak(void)
{
    /* Same arm/disarm window as the peaks: MIXer:DACPeak:CLEAR starts BOTH
     * the word-peak window and the tile[1] busy window (MIXer:BUSY?), so a
     * rig leg measures a matched interval on both instruments. */
    csd_mb_mix_busy_peak = 0u;
    for (unsigned c = 0; c < 2u; c++) {
        csd_mb_host_nonzero[c] = 0u;
        csd_mb_host_first_word[c] = 0u;
        for (unsigned n = 0; n < CSD_DACNODES; n++) {
            csd_mb_dac_peak[c][n] = 0u;
            csd_mb_dac_peak_word[c][n] = 0u;
        }
    }
    csd_mb_host_first_seen = 0u;
}

unsigned csd_mb_get_host_first_frame(void) { return csd_mb_host_first_seen; }

unsigned csd_mb_get_dac_peak(unsigned ch, unsigned node)
{
    return (ch < 2u && node < CSD_DACNODES) ? csd_mb_dac_peak[ch][node] : 0u;
}

unsigned csd_mb_get_dac_peak_word(unsigned ch, unsigned node)
{
    return (ch < 2u && node < CSD_DACNODES) ? csd_mb_dac_peak_word[ch][node] : 0u;
}

unsigned csd_mb_get_mix_frames(void) { return csd_mb_mix_frames; }

void csd_mb_note_dopvote(unsigned match, unsigned l, unsigned r)
{
    csd_mb_dop_frames++;
    if (match) { csd_mb_dop_match++; }
    csd_mb_dop_lastL = l;
    csd_mb_dop_lastR = r;
}

void csd_mb_get_dopvote(unsigned *f, unsigned *m, unsigned *l, unsigned *r)
{
    *f = csd_mb_dop_frames; *m = csd_mb_dop_match;
    *l = csd_mb_dop_lastL;  *r = csd_mb_dop_lastR;
}
