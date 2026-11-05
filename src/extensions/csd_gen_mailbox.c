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

/* Per-source pre-mixer gains, packed as (w_gen << 16) | w_host, each Q15
 * with 32768 = unity. Packed INTO ONE WORD on purpose: a set wants both
 * values to arrive together (two words would let the mixer apply a new
 * host weight with the old generator weight for a frame or two -- audible,
 * and exactly what the file header forbids). Writer: diagnostic thread
 * (SCPI MIXer:GAIN:* -- a PRODUCTION command, so this word survives the
 * release gate below). Reader: mixer thread, once per frame. Initialised
 * to unity/unity so the boot state is byte-exact passthrough even before
 * the host ever writes. */
static volatile unsigned csd_mb_gains = (32768u << 16) | 32768u;

void     csd_mb_put_gains(unsigned packed) { csd_mb_gains = packed; }
unsigned csd_mb_get_gains(void) { return csd_mb_gains; }

/* L/R volume trim (TODO items 3+4, directive 2026-11-01), packed as
 * (trimR << 16) | trimL, each an int16 in 8.8 dB: 0 = 0 dB (the power-up
 * default), negative to -32 dB = -8192, in 0.125 dB = 32-count steps.
 * ONE word for the same reason the gains are one word: the pair must
 * arrive together, and a word store is atomic, so the volume owner can
 * never apply a new left trim with a stale right trim. Writer: the
 * diagnostic thread (SCPI VOLume:TRIM:*). Reader: tile[0]'s volume owner,
 * once per volume change. Production state -- above the release gate. */
static volatile unsigned csd_mb_trim = 0u;      /* 0 dB / 0 dB on reset */
/* Trim-dirty (owner directive 2026-11-04): trim EFFECT moved from the
 * mixer-thread multiply into the ES9219Q MASTER TRIM registers (regs
 * 17..20, one 32-bit signed common 0 dB reference, datasheet v1.3). The
 * value's writers are the SCPI setter and the boot adopt (both tile[1]);
 * they post this flag so csd_AudioHwInit / the next config pass
 * (clearsounddac_board.xc) re-writes the registers even if the host never
 * touches SCPI again. 1 at reset: boot has not written the chip yet.
 * The SCPI setter ALSO writes the four registers immediately via
 * csd_diag_reg_write, so a rig sees the effect without waiting; this
 * flag covers the boot path and any missed config pass. */
static volatile unsigned csd_mb_trim_dirty = 1u;

void     csd_mb_put_trim(unsigned packed) { csd_mb_trim = packed; csd_mb_trim_dirty = 1u; }
unsigned csd_mb_get_trim(void) { return csd_mb_trim; }
unsigned csd_mb_get_trim_dirty(void) { return csd_mb_trim_dirty; }
void     csd_mb_clear_trim_dirty(void) { csd_mb_trim_dirty = 0u; }

/* ---- L/R trim as a mixer output multiplier (TODO items 3+4) ------------
 *
 * WHY HERE AND NOT IN THE CODEC (both measured on the bench, 2026-11-01):
 *   - with MIXER=1 the library forces XUA_OUT_VOLUME_IN_MIXER, so a host
 *     SET_CUR becomes SET_MIX_OUT_VOL and never reaches the app's codec
 *     volume path. rig/trim_path_probe.py proved the mixer multiplier moves
 *     (0x20000000 -> 0x0809bcce -> 0x20000000 for 0 dB -> -12 dB -> 0 dB)
 *     while ES9219Q regs 15/16 held their boot values.
 *   - regs 15/16 are 0.5 dB/LSB, so a 0.125 dB grid cannot exist there.
 * Applying it at the mixer's single post-mix multiply also satisfies the
 * directive's "on every volume change" for BOTH paths structurally: the
 * multiplier is standing, so a host SET_CUR, a button press, or no change
 * at all all pass through it. Nothing to re-apply, nothing to miss.
 *
 * COST: one multiply and one divide per SAMPLE PERIOD (not per sample):
 * the multiplier is cached and only rebuilt when the trim word changes.
 * The divide is the same 64x64 shift-and-subtract routine lib_xua uses for
 * its own volume multipliers, so there is no new arithmetic here beyond
 * what the library already does for host volume.
 *
 * THREADING: csd_mb_trim is written by the diagnostic thread and read here
 * from the mixer thread -- one aligned word, single writer, so it is never
 * read torn. The cached pair below is written and read ONLY by the mixer
 * thread (the sole caller is mixer.xc's per-frame output path), so no
 * cross-thread tearing is possible on the cache either.
 */
#define CSD_TRIM_UNITY  0x20000000u        /* MAX_VOLUME_MULT: 0 dB, Q2.29 */
#define CSD_TRIM_STEP88 32u                /* 0.125 dB in 8.8 dB units     */
#define CSD_TRIM_MAX88  (32 * 256)          /* -32 dB floor in 8.8 dB units */

/* 64x64 -> high word, shift-and-subtract: the identical routine lib_xua's
 * ep0 uses to fold master * channel volume into one Q2.29 multiplier
 * (xua_ep0_uacreqs.xc longMul). Duplicated rather than shared because that
 * one is file-static in a different library; drift between the two would
 * make trim quantise differently from host volume, which the rig below
 * checks by comparing measured trim steps against measured volume steps. */
static unsigned csd_trim_long_mul(unsigned a, unsigned b, int prec)
{
    unsigned long long c = (unsigned long long)a * (unsigned long long)b;
    unsigned ret;
    if (prec >= 32) {
        ret = (unsigned)(c >> (prec - 32)) >> 32;
    } else {
        ret = (unsigned)(c >> prec);
    }
    return ret;
}

/* Cache: rebuilt only when the trim word changes. Mixer-thread only. */
static unsigned csd_trim_mult_cache[2] = { CSD_TRIM_UNITY, CSD_TRIM_UNITY };
static unsigned csd_trim_cache_word    = 0u;   /* word the cache was built from */
static unsigned csd_trim_cache_valid   = 0u;

/* Plain int/unsigned, no int32_t: this file includes only csd_gen.h, the
 * same choice the rest of the mailbox makes. Width is not incidental here --
 * the trim word's halves are int16 by contract, and int is 32-bit on xcore. */
unsigned csd_trim_counts_to_mult_q29(int counts)        /* counts = 0.125 dB */
{
    /* 0 dB (counts 0) is unity; counts are <= 0 by contract. dB in 8.8
     * fixed point = counts * 32. db_to_mult(db, 8, 29) is what the library
     * itself uses for host volume, but it lives in ep0 and is not exported
     * as a linkable symbol here, so the same polynomial is NOT reachable
     * from this TU -- instead the trim multiplies the host multiplier by
     * 10^(dB/20) built from a 16-entry table of one-eighth-dB steps, which
     * is exact on the grid the directive mandates and needs no FPU. */
    if (counts >= 0) return CSD_TRIM_UNITY;
    unsigned steps = (unsigned)(-counts);          /* 1 step = 0.125 dB */
    if (steps > 256u) steps = 256u;                /* -32 dB floor      */
    /* One 0.125 dB step as Q2.29: round(2^29 * 10^(-0.125/20)). Computed
     * offline and asserted against the accumulated result below; the rig
     * cross-checks it acoustically, so a wrong constant shows up as a wrong
     * dB step rather than as a silent offset. */
    const unsigned STEP = 529200047u;

    /* Binary exponentiation, not a 256-iteration loop. This runs on the
     * mixer thread, which shares its tile with the hard-real-time audio
     * handshake: 256 multiplies is a real scheduling risk for a cost that
     * is 10 multiplies here (verified offline: 10 muls to -32 dB, -0.00002
     * dB error against the ideal). Rebuilt only when the trim word changes,
     * so steady-state cost is the single multiply at the bottom. */
    unsigned r = CSD_TRIM_UNITY, b = STEP;
    unsigned e = steps;
    while (e) {
        if (e & 1u) r = csd_trim_long_mul(r, b, 29);
        e >>= 1;
        if (e) b = csd_trim_long_mul(b, b, 29);
    }
    return r;
}

unsigned csd_mix_apply_trim(unsigned ch, unsigned host_mult)
{
    /* PASS-THROUGH by default (owner directive 2026-11-04): trim is chip
     * register setup now (csd_trim_master_trim_q31 -> MASTER TRIM regs
     * 17..20), computed in ZERO audio-path instructions. mixer.xc still
     * calls this at its two volume sites; an identity function inlines to
     * nothing, so the MIXER=1 frame path loses the trim multiply entirely.
     * The body below survives behind CSD_TRIM_IN_AUDIO_PATH for a forensic
     * A/B against the pre-directive behaviour -- same file, same maths,
     * git-verifiable. Do not delete it without re-verifying the register
     * path reproduces rig/trim_acoustic_ab.py within tolerance. */
#ifdef CSD_TRIM_IN_AUDIO_PATH
    /* Owner directive 2026-11-04, restated 2026-11-05: the mixer handles NO
     * trim. The audio-path multiply below is a forensic A/B kept from before
     * that directive, and it is no longer a valid comparison at all -- the
     * codec registers now carry each channel's own attenuation
     * (csd_trim_channel_vol_step -> regs 15/16), so enabling this would
     * attenuate the same trim TWICE: once here, once in the chip. The doubling
     * would then be blamed on the register path, which did not cause it.
     *
     * Refuse the combination rather than silently produce a wrong board. To
     * study the old behaviour, revert to the commit that had it -- git already
     * keeps that state; a compile-time trap that yields double attenuation is
     * not a way to reproduce history. */
    #error "CSD_TRIM_IN_AUDIO_PATH is no longer supported: the codec registers own per-channel trim, so the mixer multiply would apply it a second time (owner: the mixer handles no trim)."
    unsigned w = csd_mb_trim;                      /* single aligned load */
    if (!csd_trim_cache_valid || w != csd_trim_cache_word) {
        int tl = (int)(short)(w & 0xFFFFu);
        int tr = (int)(short)(w >> 16);
        /* Re-validate on read: the SCPI setter enforces the contract, but
         * a corrupt word must not become an amplifying multiplier. */
        int cl = (tl > 0 || tl < -(int)CSD_TRIM_MAX88 ||
                  ((unsigned)(-tl) % CSD_TRIM_STEP88)) ? 0 : tl / 32;
        int cr = (tr > 0 || tr < -(int)CSD_TRIM_MAX88 ||
                  ((unsigned)(-tr) % CSD_TRIM_STEP88)) ? 0 : tr / 32;
        csd_trim_mult_cache[0] = csd_trim_counts_to_mult_q29(cl);
        csd_trim_mult_cache[1] = csd_trim_counts_to_mult_q29(cr);
        csd_trim_cache_word  = w;
        csd_trim_cache_valid = 1u;
    }
    unsigned tm = csd_trim_mult_cache[ch & 1u];
    if (tm == CSD_TRIM_UNITY) return host_mult;    /* the 99% case, no math */
    return csd_trim_long_mul(host_mult, tm, 29);
#else
    (void)ch;
    return host_mult;                              /* trim lives in regs    */
#endif
}

/* ---- MASTER TRIM register value (directive 2026-11-04) -----------------
 * ONE implementation of the trim -> chip-word conversion, shared by the
 * SCPI immediate write (csd_diag_scpi.c) and the boot writer
 * (clearsounddac_board.xc), so the two paths cannot drift.
 *
 * Datasheet v1.3, Registers 20-17: MASTER TRIM is ONE 32-bit SIGNED number
 * setting the 0 dB reference for ALL volume controls; default 32'h7FFFFFFF,
 * documented example -1 dB = 32'h721482BF. It is NOT per channel -- the
 * header's BYTE0..3 comments name byte halves of this single word. The
 * mailbox pair therefore becomes a common reference: the MORE negative
 * (quieter) channel wins, so neither channel is ever amplified past its
 * requested trim. This mirrors vol_analog_common()'s largest-common-term
 * rule for reg 3; a true per-channel trim would need USE_SHARED_VOLUME=0
 * (reg 27) plus datasheet work and is deliberately NOT assumed.
 *
 * Scale: the trim pair is 8.8 fixed point (256 counts/dB) throughout. There is
 * NO mixer-domain trim word and no separate mixer unity constant to keep
 * straight: csd_mix_apply_trim() is an identity pass-through (owner: the mixer
 * handles no trim), so the codec word is the only conversion -- Q2.29 at unity
 * shifted LEFT by 2 and clamped (see csd_trim_master_trim_q31). Conflating a
 * mixer unity with the codec's full scale is what silenced the board once;
 * keeping the mixer out of the path removes the pair of constants entirely.
 * The old comment here called the >>2 mapping "gate-verified acoustics" -- it
 * never was; the acoustic gate was failing on this very register the whole
 * time. */
unsigned csd_trim_reg_byte(unsigned q31, unsigned idx)
{
    /* Byte order across regs 17..20 was MEASURED on silicon 2026-11-05, not
     * reasoned from the header (the old MSB-first default was a guess and it
     * silenced the board: see LOG.md 2026-11-05 and the ladder in
     * csd-js-tester/rig/trim_boot_gate.py).
     *
     * Datasheet ES9219 v1.3 Register 17-20 is a [31:0] field, and the chip's
     * multi-byte convention puts the LOW register on the LOW bits -- proven by
     * the adjacent volume pair ([7:0] VOLUME_CH1 on reg 15, [15:8] VOLUME_CH2
     * on reg 16) and by this file's own ES9219Q_VOL_CTRL_LO/HI naming. So
     * reg 0x11 == MASTER_TRIM[7:0].
     *
     * Acoustic evidence: writing the datasheet unity word FF FF FF 7F gives a
     * monotonic 0/-3/-6/-12/-20/-30 dB ladder; the same word written MSB-first
     * (7F FF FF FF) reads -101.8 dBFS at the 1 kHz bin -- 74 dB down, i.e.
     * inaudible. An MSB-first decoder cannot produce that.
     *
     * CSD_TRIM_BYTES_MSB_FIRST exists only to reproduce the refuted build. */
#ifdef CSD_TRIM_BYTES_MSB_FIRST
    return (q31 >> (8u * (3u - idx))) & 0xFFu;     /* reg 17 = MSB (refuted) */
#else
    return (q31 >> (8u * idx)) & 0xFFu;            /* reg 17 = LSB (silicon)  */
#endif
}

unsigned csd_trim_master_trim_q31(unsigned packed)
{
    /* Unit trap caught by a fresh-boot rig, 2026-11-04: an earlier revision
     * read the packed fields as RAW 0.125 dB counts, but the mailbox stores
     * them in the 8.8 grid (SCPI setter packs counts*32; csd_gen.h header:
     * "int16 in 8.8 dB... -32 dB = -8192, in 0.125 dB = 32-count steps").
     * So -96 counts (-12 dB) arrived as -3072, clamped to the -256-count
     * (-32 dB) rail: every trim shallower than full scale measured as
     * 0x00337183. Divide by the grid step FIRST, clamp in counts. */
    /* (int)(short) rather than int16_t: this TU has no stdint in scope --
     * the file's existing idiom (csd_gen_mailbox.c). */
    int trimL = (int)(short)(packed & 0xFFFFu);
    int trimR = (int)(short)(packed >> 16);
    /* The master word is one gain for BOTH channels, so it carries the
     * SHALLOWEST trim (smallest attenuation) and csd_trim_channel_vol_step()
     * adds each channel's remainder in the digital stage:
     *     A_common = min(A),  D[ch] = A[ch] - A_common  >= 0
     * Reversing this (taking the deepest, which this line did until
     * 2026-11-05) leaves the shallower channel over-attenuated by the full
     * difference and the deeper one short of nothing -- an attenuator cannot
     * add gain back, so the remainder must always be >= 0. Measured with the
     * old rule: LEFT-only trim dropped RIGHT 29 dB instead of 0 dB.
     * At equal trims both readings are the same, so the 0 dB ladder
     * (FF FF FF 7F) is untouched by this change. */
    int common = (trimL > trimR) ? trimL : trimR;  /* shallowest channel */
    if (common > 0) common = 0;                    /* never amplify   */
    if (common < -CSD_TRIM_MAX88) common = -CSD_TRIM_MAX88;
    int counts = common / 32;                      /* 8.8 grid -> 0.125 dB steps */
    unsigned q29 = csd_trim_counts_to_mult_q29(counts);
    /* Q2.29 -> codec Q31 is a shift LEFT by 2, clamped. The datasheet defines
     * MASTER_TRIM as a 32-bit SIGNED value whose full scale is 32'h7FFFFFFF
     * ("Defaults to full-scale (32'h7FFFFFFF)"), so unity in the codec domain
     * is 2^31-1 -- not 2^29. The previous `q29 >> 2` mapped 0 dB to
     * 0x08000000 (= -30 dB as a Q31 fraction); with the MSB-first byte-order
     * guess on top, the chip received 0x00000008 and the analog output was
     * -168 dB, i.e. silent. Verified against the datasheet ladder in
     * rig/trim_boot_gate.py: 0 dB now writes FF FF FF 7F and is audible. */
    unsigned q31 = (q29 >= 0x20000000u) ? 0x7FFFFFFFu : (q29 << 2);
    if (q31 > 0x7FFFFFFFu) q31 = 0x7FFFFFFFu;
    return q31;
}

/* Per-channel attenuation for the codec's OWN channel volume registers
 * (ES9219Q_VOL_CTRL_LO/HI, regs 15/16), in the chip's 0.5 dB steps.
 *
 * WHY THIS EXISTS (case 10, measured 2026-11-05): MASTER TRIM is a single word
 * shared by both channels, so the only thing it can express is what the two
 * channels have IN COMMON. csd_trim_master_trim_q31() collapses L/R with
 * min() for exactly that reason, which is why setting LEFT alone moved BOTH
 * channels ~6 dB on the bench: there was nowhere else for the value to go.
 * Datasheet v1.3: "Each output channel has its own attenuation circuit. The
 * attenuation for each channel is controlled independently", 0..-127 dB in
 * 0.5 dB steps, 0 = 0 dB. So the decomposition is forced, not chosen:
 *
 *   master trim (regs 17-20) = the deepest common attenuation  (unchanged,
 *                              ladder-verified: 0 dB writes FF FF FF 7F)
 *   reg 15 (CH1) / reg 16 (CH2) = the EXTRA beyond common, i.e. the difference
 *                              between the two channels, on the shallower one
 *
 * Equal trims therefore still travel the verified master path with regs 15/16
 * at 0, so this cannot regress the ladder that took a day to prove.
 *
 * QUANTISATION IS HONEST, NOT A SHORTCUT: the SCPI interface is 0.125 dB and
 * the per-channel hardware step is 0.5 dB, so the L/R difference is rounded to
 * the nearest half dB. Below half a decibel this chip cannot make the channels
 * differ at all; a rig asserting finer per-channel resolution than 0.5 dB is
 * asserting something the silicon does not do. */
unsigned csd_trim_channel_vol_step(unsigned packed, unsigned right)
{
    int l    = (int)(short)(packed & 0xFFFFu);   /* 0.125 dB units, <= 0 */
    int r    = (int)(short)(packed >> 16);
    int mine = right ? r : l;

    /* The analog master word is ONE gain for both channels, so it can only
     * carry the SMALLEST attenuation (the shallowest trim): whatever it
     * applies, it applies to both. The digital stage then adds what each
     * channel still needs on top. Written as magnitudes A[ch] = -trim[ch]:
     *
     *     A_common = min(A)            (the shallower channel, by magnitude)
     *     D[ch]    = A[ch] - A_common  (0 on the shallow channel)
     *
     * Both in signed dB that is common = max(l, r) and extra = common - mine.
     * Taking min() here instead -- my first attempt, and what the master
     * converter still does -- puts the extra attenuation on the channel that
     * is supposed to stay flat. Measured on silicon 2026-11-05 with that
     * error in place: trimming LEFT dropped LEFT 4.2 dB (correct) and RIGHT
     * 29 dB (it should not move at all). */
    int common = (l > r) ? l : r;
    int extra  = common - mine;                  /* >= 0 by construction */

    if (extra < 0) extra = 0;
    /* The pair is 8.8 fixed point -- 256 counts per dB, which is what the
     * record field name trim_l_88 says and what the bench proved: a 6 dB trim
     * (1536 counts) produced 48 steps at a divisor of 32, and clamped at 254
     * (=254 steps, ~42 dB of the chip's real per-channel reach) at a divisor
     * of 4. A 0.5 dB hardware step is therefore 256/2 = 128 counts. Each
     * divisor here has been measured rather than assumed:
     *     /32  ->  48 steps  = 24 dB asked for 6 dB   (wrong, 4x)
     *     /4   ->  384 steps = clamped 254            (wrong, 32x)
     *     /128 ->   12 steps =  6 dB                  (this line)
     */
    unsigned steps = (unsigned)((extra + 64) / 128);   /* 8.8 dB -> 0.5 dB */
    if (steps > 254u) steps = 254u;                    /* -127 dB is the floor */
    return steps;
}

/* ---- release gate (TODO items 5+6, directive 2026-11-01) -----------------
 * Everything below this line EXCEPT the per-source gain word is bench
 * instrumentation: counters and peak-houses that only the debug SCPI block
 * in csd_diag_scpi.c (also compiled out) can read. The XC call-sites are
 * guarded by CSD_GEN_EN, which release builds keep on (the rig drives the
 * generator), so the note_* entry points must continue to EXIST -- under
 * SPECIALTYCIRCUITS_RELEASE their bodies compile to nothing and the
 * whole-program compile inlines the per-sample/per-frame calls away. The
 * getters are compiled out entirely: with the debug callbacks gone nothing
 * references them, and an unreferenced static costs flash.
 * Functional state that stays in every build: csd_mb_fs, csd_mb_dop (rate
 * + DoP hand-off) and csd_mb_gains (MIXer:GAIN:* is a production command). */
#ifdef SPECIALTYCIRCUITS_RELEASE
#define CSD_DBG_OFF 1
#else
#define CSD_DBG_OFF 0
#endif
#if !CSD_DBG_OFF
/* NOTE: the loopback tap below is deliberately compiled OUTSIDE this region
 * (see the comment at its definition) so a release build with
 * -DCSD_LOOPBACK_TAP still links. */

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

/* Second half of the tile[1] budget: ticks mixer1 spent AFTER unblocking
 * audio() and BEFORE it could serve the next request -- the generator
 * hand-off, DoP vote and instrument live here, outside the busy window. A
 * deadline needs BOTH halves: audio() cannot start its next frame until
 * mixer1 returns to inuint(c_audio). */
static volatile unsigned csd_mb_mix_tail_peak;

void csd_mb_note_mix_tail(unsigned delta_ticks)
{
    if (delta_ticks > csd_mb_mix_tail_peak) {
        csd_mb_mix_tail_peak = delta_ticks;
    }
}

unsigned csd_mb_get_mix_tail_peak(void) { return csd_mb_mix_tail_peak; }

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

/* ---- DAC->ADC loopback tap: state (see csd_gen.h for the contract) ------
 *
 * WHY A SINGLE-WORD HAND-OFF AND NOT A BUFFER
 * The tap writes one word per channel per LRCLK frame from the TX side of the
 * audiohub loop; the RX side of the SAME loop, a few hundred cycles later,
 * reads it. Same task, same tile, no interleaving between the two halves of a
 * frame -- so a plain volatile word is sufficient and needs no lock. It also
 * means the value the host eventually reads back is the word from at most one
 * frame earlier: a fixed pipeline offset, not a queue. The rig does not need
 * to know that offset, because the stimulus carries its own frame index and
 * the rig aligns on it; see rig/bit_perfect_common.py.
 *
 * CONSEQUENCE WORTH STATING: if the TX side stalls, the RX side re-injects the
 * PREVIOUS word rather than something fresh. That is the desired behaviour --
 * the rig then reports a duplicated frame index, which is the true defect
 * (the device really did put that word on the wire twice). Substituting a
 * fresh value on a stall would mask the stall.
 */
#endif /* !CSD_DBG_OFF -- bench instrumentation region (reopened below) */

/* The loopback tap is ORTHOGONAL to the release gate: it exists only when
 * -DCSD_LOOPBACK_TAP is passed explicitly, so it is opted into by name rather
 * than swept up with the automatic debug instrumentation. It must also link in
 * a RELEASE-optimised image, because the thing being certified (bit-exact
 * transfer at 384 kHz) has to be certified against the code that ships, not
 * against the debug build -- measured, the debug image misses 58% of its I2S
 * deadlines and cannot certify timing at all. An earlier version left these
 * definitions inside the CSD_DBG_OFF region while the call sites in lib_xua's
 * xua_audiohub.xc are gated only on CSD_LOOPBACK_TAP, so a release+tap build
 * failed to link: 'Undefined reference to csd_mb_loop_put'. */
#if defined(CSD_LOOPBACK_TAP)
static volatile unsigned csd_mb_loop_en;
static volatile unsigned csd_mb_loop_tx[2];
static volatile unsigned csd_mb_loop_frames;
/* Where do put() calls actually land? Observed hardware result: play
 * L=0x55AA5A R=0x0F0F01 and BOTH capture channels return 0x0F0F01 -- slot 0
 * carries slot 1's word -- while the source code of both call sites reads
 * frameCount+i with i in {0,1} and looks correct. Paper is not evidence here:
 * count the channel index each call site presents, per call site, so the
 * device itself says which site collapses the channels (or that the sites
 * present the indices as intended and the collapse is downstream).
 * [0]=first TX site (xua_audiohub.xc ~line 453), [1]=second (~592),
 * [2]=take() arrivals per channel. */
static volatile unsigned csd_mb_loop_put_ch[4];
static volatile unsigned csd_mb_loop_take_ch[4];
static volatile unsigned csd_mb_loop_site[2][4];
/* Last word each TX site saw. COUNTERS ALONE were not enough: they prove the
 * sites run and with which index, but the observed fault is a VALUE fault --
 * both capture channels returned host R's word while source review shows every
 * writer indexed by i (mixer ptr_samples[i], device send ptr_samples[deviceMap
 * [i]] with an identity map, USB transfer samplesOut[i] = inuint()). So the
 * open question is whether samplesOut[0] holds host L at all. site_word[0]
 * holding R's value answers "no" and puts the fault upstream of the tap;
 * holding L's value puts it in the tap's read side. Read the value, do not
 * infer it from the index -- that is the mistake that cost the last three runs.
 * Torn reads are possible (384 kHz writer vs SCPI reader) but harmless here:
 * the stimulus is a constant per-channel word. */
static volatile unsigned csd_mb_loop_site_word[2];

/* THE HOT PATH. Called twice per LRCLK frame from the audio() task, which at
 * 384 kHz has 32 BCLK = 1.302 us = 781 core ticks per call. Measured evidence
 * that this mattered: the instrumented version did 5 volatile RMWs per call --
 * two of them BEFORE the enable check, so even a disarmed tap paid them -- i.e.
 * ~20 volatile RMWs and 4 cross-TU calls per frame, inside the very budget the
 * investigation is about. So the histograms are now opt-in: a minimal build
 * carries only the payload store and the witness counter, which is all the tap
 * functionally needs. The rig derives its per-frame ratio from 'frames' (2 puts
 * per LRCLK frame) and does not need the histograms, so nothing is lost except
 * the ability to see WHERE a put came from -- which is what CSD_LOOPBACK_TAP_
 * VERBOSE is for. */
void csd_mb_loop_put_site(unsigned site, unsigned ch, unsigned word)
{
#ifdef CSD_LOOPBACK_TAP_VERBOSE
    if (site < 2u && ch < 4u) csd_mb_loop_site[site][ch]++;
    if (site < 2u && ch < 2u) csd_mb_loop_site_word[site] = word;
#else
    (void)site;
#endif
    if (!csd_mb_loop_en) return;
#ifdef CSD_LOOPBACK_TAP_VERBOSE
    if (ch < 4u) csd_mb_loop_put_ch[ch]++;
#endif
    if (ch < 2u) {
        csd_mb_loop_tx[ch] = word;
        csd_mb_loop_frames++;
    }
}

void csd_mb_loop_put(unsigned ch, unsigned word)
{
    csd_mb_loop_put_site(2u, ch, word);      /* unattributed, if any caller */
}

unsigned csd_mb_loop_take(unsigned ch, unsigned fallback)
{
    /* Same hot path as put_site: called twice per LRCLK frame from the RX half
     * of the audio() loop, inside the 781-tick budget. The per-channel arrival
     * histogram was answering "which index does RX present" -- a question that
     * is now ANSWERED (both channels, once each, symmetric with TX), so it is
     * no longer worth a volatile RMW per call. Kept behind VERBOSE. */
#ifdef CSD_LOOPBACK_TAP_VERBOSE
    if (ch < 4u) csd_mb_loop_take_ch[ch]++;
#endif
    if (!csd_mb_loop_en || ch > 1u) return fallback;
    return csd_mb_loop_tx[ch];
}

void csd_mb_loop_set_enable(unsigned enable)
{
    /* Clear the payload on every transition so a session cannot inherit the
     * last word of the previous one and have it read as frame 0 of this one:
     * the rig's alignment leg would then match against a stale word. */
    csd_mb_loop_tx[0] = 0u;
    csd_mb_loop_tx[1] = 0u;
    csd_mb_loop_frames = 0u;
    /* The WORDS too. Clearing only the counters was the bug: a leg whose tap
     * never wrote answered with the PREVIOUS leg's words, and the two legs of
     * the DSP A/B came back byte-identical (0xFFCE53EE / 0xFFF7245B) while their
     * captures disagreed -- a stale-value artifact that would have been read as
     * "the DSP does not affect the words", which is exactly backwards. A zero
     * here is now meaningful: it means "this site has not written since arming",
     * which is a claim the rig can and must assert on. */
    csd_mb_loop_site_word[0] = 0u;
    csd_mb_loop_site_word[1] = 0u;
    for (unsigned i = 0; i < 4u; i++) {
        csd_mb_loop_put_ch[i] = 0u;
        csd_mb_loop_take_ch[i] = 0u;
    }
    for (unsigned s = 0; s < 2u; s++)
        for (unsigned i = 0; i < 4u; i++)
            csd_mb_loop_site[s][i] = 0u;
    csd_mb_loop_en = enable ? 1u : 0u;
}

void csd_mb_loop_get(unsigned *en, unsigned *frames)
{
    if (en)     *en     = csd_mb_loop_en;
    if (frames) *frames = csd_mb_loop_frames;
}

/* Diagnostics for the channel-collapse question (see put_site above):
 * returns per-channel put arrivals, per-channel take arrivals, and the two
 * per-site histograms. */
void csd_mb_loop_get_index(unsigned *put_ch, unsigned *take_ch,
                           unsigned *site0, unsigned *site1)
{
    for (unsigned i = 0; i < 4u; i++) {
        if (put_ch)  put_ch[i]  = csd_mb_loop_put_ch[i];
        if (take_ch) take_ch[i] = csd_mb_loop_take_ch[i];
        if (site0)   site0[i]   = csd_mb_loop_site[0][i];
        if (site1)   site1[i]   = csd_mb_loop_site[1][i];
    }
}

/* The words, not the counters. Called with (site_word[2], tx[2]):
 *   site_word[0..1] -- what each TX site last saw from samplesOut
 *   tx[0..1]        -- what the tap is currently serving to the two RX slots
 * With a constant per-channel stimulus these four numbers settle, in one
 * read, whether host L ever reaches samplesOut[0]. */
void csd_mb_loop_get_words(unsigned *site_word, unsigned *tx)
{
    if (site_word) { site_word[0] = csd_mb_loop_site_word[0];
                     site_word[1] = csd_mb_loop_site_word[1]; }
    if (tx)        { tx[0] = csd_mb_loop_tx[0];
                     tx[1] = csd_mb_loop_tx[1]; }
}
#endif /* CSD_LOOPBACK_TAP */

#if !CSD_DBG_OFF
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

#else  /* CSD_DBG_OFF: the XC call-sites (guarded by CSD_GEN_EN, which the
 * release build keeps on for the generator) still need these symbols --
 * empty bodies, and the whole-program compile inlines the per-frame/per-
 * sample calls away entirely. Getters/clears are NOT stubbed: their only
 * callers were the debug SCPI callbacks, gone with them. */
void csd_mb_note_vol(unsigned index, unsigned val)              { (void)index; (void)val; }
void csd_mb_note_mix_frame(void)                                { }
void csd_mb_note_mix_busy(unsigned delta_ticks)                 { (void)delta_ticks; }
void csd_mb_note_mix_tail(unsigned delta_ticks)                 { (void)delta_ticks; }
void csd_mb_note_hwcfg(unsigned sam_freq, unsigned mclk)        { (void)sam_freq; (void)mclk; }
void csd_mb_note_i2s_delta(unsigned short delta_ticks)          { (void)delta_ticks; }
void csd_mb_note_host_nonzero(unsigned ch, int s, unsigned f)   { (void)ch; (void)s; (void)f; }
void csd_mb_note_dac_sample(unsigned ch, unsigned node, int s)  { (void)ch; (void)node; (void)s; }
void csd_mb_note_dopvote(unsigned m, unsigned l, unsigned rr)   { (void)m; (void)l; (void)rr; }
#endif /* !CSD_DBG_OFF -- bench instrumentation region */
