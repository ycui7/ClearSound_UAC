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
