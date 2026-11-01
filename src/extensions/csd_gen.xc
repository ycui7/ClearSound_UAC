// CSD function generator -- audio side.
//
// THE ONLY translation unit that includes csd_gen_core.h. That is a memory
// constraint, not a preference: the tables in csd_gen_tables.h are file-scope
// `static const`, so a second includer emits a second ~20 KB copy into a
// tile's 512 KB reserved memory. If you are tempted to include the core from
// a test or a second module, call the wrappers here instead.
//
// Threading: csd_dsp.xc established by measurement (2026-10-30, the bump
// instrument: the host saw it advance 1->2->3 across three preset commands)
// that the diagnostic thread and the audiohub thread share memory, and that
// the safe pattern is volatile-accessed pending fields adopted at a frame
// boundary. That is what this file does. The descriptor itself is copied in
// one shot by the audio thread, so a command that lands mid-frame cannot
// produce a waveform assembled from two different commands.
#include "csd_gen.h"
#include "csd_gen_core.h"

/* Cross-check the duplicated numbering (csd_gen.h vs the core's enums). A
 * mismatch is not a compile error otherwise -- it is two waveforms quietly
 * swapping places. */
/* Compile-time cross-checks of the duplicated numbering (csd_gen.h vs the
 * core's enums). _Static_assert is NOT usable: XC's pca frontend parses the
 * call as a function declaration and dies with "parse error before numeric
 * constant" (measured, first target build). The negative-size array typedef
 * is the portable predecessor -- a false condition is a compile error in any
 * C frontend, and there is no code to run, so a swapped SINE/SQUARE can never
 * ship. */
#define CSD_GEN_STATIC_ASSERT(cond, tag) \
    typedef char csd_gen_assert_##tag[(cond) ? 1 : -1]

CSD_GEN_STATIC_ASSERT(CSD_GEN_MODE_OFF       == (unsigned)CSD_GEN_OFF,       off);
CSD_GEN_STATIC_ASSERT(CSD_GEN_MODE_SINE      == (unsigned)CSD_GEN_SINE,      sine);
CSD_GEN_STATIC_ASSERT(CSD_GEN_MODE_SQUARE    == (unsigned)CSD_GEN_SQUARE,    square);
CSD_GEN_STATIC_ASSERT(CSD_GEN_MODE_SWEEP     == (unsigned)CSD_GEN_SWEEP,     sweep);
CSD_GEN_STATIC_ASSERT(CSD_GEN_MODE_MULTITONE == (unsigned)CSD_GEN_MULTITONE, multitone);
CSD_GEN_STATIC_ASSERT(CSD_GEN_MODE_NOISE     == (unsigned)CSD_GEN_NOISE,     noise);
CSD_GEN_STATIC_ASSERT(CSD_GEN_DOMAIN_LIN     == (unsigned)CSD_GEN_LIN,       lin);
CSD_GEN_STATIC_ASSERT(CSD_GEN_DOMAIN_LOG     == (unsigned)CSD_GEN_LOG,       log);
CSD_GEN_STATIC_ASSERT(CSD_GEN_MAX_PERIOD_MS  == (unsigned)CSD_GEN_PERIOD_MAX_MS, period);
CSD_GEN_STATIC_ASSERT(CSD_GEN_MAX_TONES_PUB  == CSD_GEN_MAX_TONES, tones);
CSD_GEN_STATIC_ASSERT(CSD_GEN_HZ_SCALE       == (1u << CSD_GEN_HZ_SHIFT), hzscale);

/* Supported rates: the UAC altsettings this board advertises (csd_dsp_bq_rates
 * is indexed the same way). Anything else is refused at arm time -- see the
 * note in csd_gen.h about a generator that happily arms at a non-rate. */
static const unsigned csd_gen_rates[] = {
    44100u, 48000u, 88200u, 96000u, 176400u, 192000u
};

int csd_gen_fs_supported(unsigned fs_hz)
{
    for (unsigned i = 0; i < sizeof csd_gen_rates / sizeof csd_gen_rates[0]; i++)
        if (csd_gen_rates[i] == fs_hz) return 1;
    return 0;
}

/* ---- cross-thread state ------------------------------------------------
 *
 * Written by the calling (SCPI/MIDI) thread, read by the audiohub thread.
 * Volatile accessors because XC accepts `volatile` only on unsafe pointer
 * targets, and a plain global read on XCore may be served from a register
 * across a scheduling point -- the failure that made these accessors
 * mandatory for csd_dsp's preset applies verbatim here.
 *
 * `gen` is touched by the audio thread ONLY, which is what makes a 200-byte
 * struct safe to hand over without a lock: the caller writes into `pend`, a
 * separate object, and sets a command counter. The audio thread copies, then
 * marks it adopted. csd_gen_cmd != csd_gen_adopted therefore means "waiting
 * for a frame boundary", which the host can read back.
 */
/* Both start all-zero, and that is correct rather than sloppy:
 *   - mode 0 IS CSD_GEN_OFF, so nothing is emitted and the audio buffer is
 *     untouched until a command arrives;
 *   - the live struct is only ever SAMPLED after an arm, and an arm copies in
 *     a descriptor that csd_gen_reset() seeded (rng non-zero, amp at -6 dBFS),
 *     so the core is the single source of those seeds. Writing the same
 *     constants here as literals would create exactly the duplicate-constant
 *     pair that has bitten this module twice already -- SQ_HARMONICS and the
 *     mode selectors -- and csd_gen_apply_fs() re-seeds rng defensively too.
 *   - csd_gen_notify_rate() early-returns while OFF, and csd_gen_request_off()
 *     falls back to a default fs when this is still zero, so the idle struct is
 *     never used as if it described a wave.
 */
static csd_gen_t         csd_gen;         /* audio thread's live generator   */
static csd_gen_t         csd_gen_pend;    /* staged descriptor, caller writes */
/* Plain unsigned, NOT `static volatile unsigned`: XC rejects volatile on a
 * plain object ("can only specify volatile on unsafe pointer target type",
 * measured on the first target build). Volatility lives in the accessor's
 * cast instead, which is exactly what csd_dsp.xc does for csd_dsp_bump and
 * why those helpers exist in this codebase at all. The two spellings look
 * equivalent in C and are not in XC. */
static unsigned csd_gen_cmd;              /* incremented by the caller        */
static unsigned csd_gen_adopted;          /* incremented by the audio thread  */
static unsigned csd_gen_rejects;
static unsigned csd_gen_last_reject;      /* a CSD_GEN_REJ_* value            */

/* Per-frame accounting of the DSD/DoP injection gate, counted ONLY while the
 * generator is armed -- that is the case the rig must see, and it keeps the
 * OFF path (the product path) free of the extra work. The mic capture path
 * cannot disprove injection at DoP rates: the EARS codec is S24_3LE at 48 kHz
 * fixed, so arecord -r 176400 silently resamples (2 s asked, 96000 frames
 * back) and any spectrum computed against the REQUESTED rate is fiction --
 * exactly the false FAIL this counter replaces. A count taken on the audio
 * thread at the decision point is the direct measurement: emitted vs
 * suppressed-by-dsd vs suppressed-by-dop. */
static unsigned csd_gen_n_emit;           /* armed: wave written this frame   */
static unsigned csd_gen_n_sup_dsd;        /* armed: skipped, DSD/native mode  */
static unsigned csd_gen_n_sup_dop;        /* armed: skipped, DoP latch on     */

static unsigned csd_gen_load(const unsigned *p)
{
    unsigned x;
    unsafe { x = *(volatile unsigned * unsafe)p; }
    return x;
}

static void csd_gen_store(unsigned *p, unsigned x)
{
    unsafe { *(volatile unsigned * unsafe)p = x; }
}

static unsigned csd_gen_next_cmd(void)
{
    unsigned c = csd_gen_load(&csd_gen_cmd) + 1u;
    csd_gen_store(&csd_gen_cmd, c);
    return c;
}

static void csd_gen_reject(unsigned why)
{
    /* Sequenced through a temporary. `store(&x, load(&x) + 1)` is rejected by
     * XC with "causes an ambiguous evaluation" -- the volatile read and the
     * volatile write of the same variable in one expression have no defined
     * order, and the compiler refuses rather than guessing. csd_dsp.xc hits
     * the identical wall on csd_dsp_bump and documents it the same way. */
    unsigned n = csd_gen_load(&csd_gen_rejects) + 1u;
    csd_gen_store(&csd_gen_last_reject, why);
    csd_gen_store(&csd_gen_rejects, n);
}

/* Validate on the CALLING thread so a bad SCPI argument gets an error reply
 * now, not silence two frames later. Mirrors csd_gen_apply_fs()'s checks
 * rather than relying on them, because the core reports one generic -1 and the
 * SCPI layer has to distinguish an illegal frequency from an illegal period. */
static int csd_gen_check_freq(unsigned f_q8, unsigned fs_hz)
{
    if (f_q8 < CSD_GEN_HZ_ONE) return 0;                    /* 1 Hz floor  */
    /* Nyquist in Q8, computed the same way the core does it so the two cannot
     * disagree about whether 20 kHz at 44.1 kHz is legal. */
    unsigned nyq = ((unsigned)((uint32_t)fs_hz >> 1)) << CSD_GEN_HZ_SHIFT;
    return f_q8 <= nyq;
}

int csd_gen_request_arm(unsigned mode, unsigned fs_hz,
                        unsigned f_start_q8, unsigned f_stop_q8,
                        unsigned domain, unsigned amp,
                        unsigned period_ms, int *why)
{
    if (why) *why = CSD_GEN_REJ_NONE;
    if (!csd_gen_fs_supported(fs_hz)) {
        csd_gen_reject(CSD_GEN_REJ_FS);
        if (why) *why = CSD_GEN_REJ_FS;
        return -1;
    }
    if (mode > CSD_GEN_MODE_NOISE) {
        csd_gen_reject(CSD_GEN_REJ_MODE);
        if (why) *why = CSD_GEN_REJ_MODE;
        return -1;
    }
    if (mode == CSD_GEN_MODE_SWEEP && domain > CSD_GEN_DOMAIN_LOG) {
        csd_gen_reject(CSD_GEN_REJ_DOMAIN);
        if (why) *why = CSD_GEN_REJ_DOMAIN;
        return -1;
    }
    if (amp < CSD_GEN_AMP_MIN || amp > CSD_GEN_AMP_MAX) {
        csd_gen_reject(CSD_GEN_REJ_AMP);
        if (why) *why = CSD_GEN_REJ_AMP;
        return -1;
    }
    if (period_ms > CSD_GEN_MAX_PERIOD_MS) {
        csd_gen_reject(CSD_GEN_REJ_PERIOD);
        if (why) *why = CSD_GEN_REJ_PERIOD;
        return -1;
    }
    /* f_stop_q8 == 0 means "not a sweep": the core substitutes f_a. Any other
     * value must be in range, and it may be above, below or equal to f_a --
     * all three are valid (steady, falling, rising). */
    if (!csd_gen_check_freq(f_start_q8, fs_hz)) {
        csd_gen_reject(CSD_GEN_REJ_FREQ_RANGE);
        if (why) *why = CSD_GEN_REJ_FREQ_RANGE;
        return -1;
    }
    if (f_stop_q8 != 0u && !csd_gen_check_freq(f_stop_q8, fs_hz)) {
        csd_gen_reject(CSD_GEN_REJ_FREQ_RANGE);
        if (why) *why = CSD_GEN_REJ_FREQ_RANGE;
        return -1;
    }

    /* Stage into the pending descriptor. csd_gen_wave() validates again and
     * refuses; if it does, nothing is published, so the running generator
     * keeps playing its previous wave rather than going to silence.
     *
     * csd_gen_reset() first: it is the core's own seeder (rng non-zero, amp at
     * -6 dBFS, tone count in range), so calling it here keeps those values in
     * one place instead of repeating them as literals in this file. */
    int core_why = 0;
    csd_gen_reset(&csd_gen_pend);
    if (csd_gen_wave(&csd_gen_pend, fs_hz, mode,
                     (uint32_t)f_start_q8, (uint32_t)f_stop_q8,
                     domain, (uint32_t)amp, (uint64_t)period_ms,
                     &core_why) != 0) {
        csd_gen_reject(CSD_GEN_REJ_FREQ_RANGE);
        if (why) *why = CSD_GEN_REJ_FREQ_RANGE;
        return -1;
    }
    csd_gen_next_cmd();
    return 0;
}

int csd_gen_request_tones(unsigned fs_hz, unsigned tones,
                          const unsigned *f_q8, unsigned amp,
                          unsigned period_ms, int *why)
{
    if (why) *why = CSD_GEN_REJ_NONE;
    if (!csd_gen_fs_supported(fs_hz)) {
        csd_gen_reject(CSD_GEN_REJ_FS);
        if (why) *why = CSD_GEN_REJ_FS;
        return -1;
    }
    if (tones < CSD_GEN_MIN_TONES || tones > CSD_GEN_MAX_TONES) {
        csd_gen_reject(CSD_GEN_REJ_TONES);
        if (why) *why = CSD_GEN_REJ_TONES;
        return -1;
    }
    if (amp < CSD_GEN_AMP_MIN || amp > CSD_GEN_AMP_MAX) {
        csd_gen_reject(CSD_GEN_REJ_AMP);
        if (why) *why = CSD_GEN_REJ_AMP;
        return -1;
    }
    if (period_ms > CSD_GEN_MAX_PERIOD_MS) {
        csd_gen_reject(CSD_GEN_REJ_PERIOD);
        if (why) *why = CSD_GEN_REJ_PERIOD;
        return -1;
    }
    uint32_t fq[CSD_GEN_MAX_TONES];
    for (unsigned i = 0; i < tones; i++) {
        if (!csd_gen_check_freq(f_q8[i], fs_hz)) {
            csd_gen_reject(CSD_GEN_REJ_TONES);
            if (why) *why = CSD_GEN_REJ_TONES;
            return -1;
        }
        fq[i] = (uint32_t)f_q8[i];
    }

    int core_why = 0;
    csd_gen_reset(&csd_gen_pend);
    /* Frequencies first, then the descriptor: csd_gen_tones() stores f and
     * csd_gen_apply_fs() derives the steps, and the core was fixed so the
     * order of the two calls no longer matters. Asserting it here in words
     * because the silent version of this bug was a multitone that produced
     * nothing while every other mode passed. */
    if (csd_gen_wave(&csd_gen_pend, fs_hz, CSD_GEN_MULTITONE,
                     fq[0], fq[0], CSD_GEN_LIN,
                     (uint32_t)amp, (uint64_t)period_ms, &core_why) != 0) {
        csd_gen_reject(CSD_GEN_REJ_FREQ_RANGE);
        if (why) *why = CSD_GEN_REJ_FREQ_RANGE;
        return -1;
    }
    if (csd_gen_tones(&csd_gen_pend, fq, tones) != 0) {
        csd_gen_reject(CSD_GEN_REJ_TONES);
        if (why) *why = CSD_GEN_REJ_TONES;
        return -1;
    }
    csd_gen_next_cmd();
    return 0;
}

void csd_gen_request_off(void)
{
    int why = 0;
    csd_gen_reset(&csd_gen_pend);
    (void)csd_gen_wave(&csd_gen_pend, csd_gen.fs ? csd_gen.fs : 48000u,
                       CSD_GEN_OFF, CSD_GEN_HZ_ONE, 0u, CSD_GEN_LIN,
                       CSD_GEN_AMP_MIN, 0ull, &why);
    csd_gen_next_cmd();
}

/* ---- audio thread ------------------------------------------------------ */

/* The frame hook reports what the gate decided. `why`: 0 = wave emitted,
 * 1 = suppressed because the stream is DSD/native, 2 = suppressed by the DoP
 * latch. Plain load/store increments: only the audio thread writes these. The
 * SCPI reader takes them as three independent monotones, not a consistent
 * snapshot -- the rig only ever needs each to have advanced. */
void csd_gen_note(unsigned why)
{
    unsigned v;
    if (why == 0u) {
        v = csd_gen_load(&csd_gen_n_emit) + 1u;
        csd_gen_store(&csd_gen_n_emit, v);
    } else if (why == 1u) {
        v = csd_gen_load(&csd_gen_n_sup_dsd) + 1u;
        csd_gen_store(&csd_gen_n_sup_dsd, v);
    } else {
        v = csd_gen_load(&csd_gen_n_sup_dop) + 1u;
        csd_gen_store(&csd_gen_n_sup_dop, v);
    }
}

void csd_gen_apply_pending(void)
{
    unsigned c = csd_gen_load(&csd_gen_cmd);
    if (c == csd_gen_load(&csd_gen_adopted)) return;

    /* Copy the whole descriptor in one go, on this thread. A torn read here
     * would be a wave made of two commands, which is why the caller stages
     * into a separate object instead of editing csd_gen in place. */
    csd_gen = csd_gen_pend;
    csd_gen_store(&csd_gen_adopted, c);
}

void csd_gen_fill(int samp[2])
{
    if (csd_gen.mode == (unsigned)CSD_GEN_OFF) {
        samp[0] = 0;
        samp[1] = 0;
        return;
    }
    /* unsafe arrays: the oscillator loops index fixed-size arrays with a
     * runtime bound. The bounds are enforced by construction (loop limits are
     * the array sizes, and the harmonic loop breaks on Nyquist), and XC's
     * checked indexing here would put a compare per term in the 21 us frame. */
    unsafe {
        csd_gen_sample(&csd_gen, (int32_t *)samp);
    }
}

int csd_gen_inject(unsigned *s)
{
    /* Adopt first, then emit: a command posted between two frames takes
     * effect at the boundary, so a burst's first sample is the first sample of
     * the wave rather than a frame of the previous wave. */
    csd_gen_apply_pending();

    if (csd_gen.mode == (unsigned)CSD_GEN_OFF) return 0;

    int v[2];
    csd_gen_fill(v);
    s[0] = (unsigned)v[0];
    s[1] = (unsigned)v[1];
    return 1;
}

/* ---- host-visible state ------------------------------------------------
 *
 * Every getter below reads csd_gen -- the ADOPTED generator, the one the
 * audio thread is filling -- never csd_gen_pend. These run on the calling
 * thread while the audio thread writes csd_gen, so each returns a
 * word-consistent snapshot of one field, not a coherent descriptor. That is
 * sufficient for the SCPI contract (the host reads one value per query) and
 * is stated because a future caller that wants the whole descriptor at once
 * would need the cmd/adopted handshake around it, not these.
 */

unsigned csd_gen_state_mode(void)     { return (unsigned)csd_gen.mode; }

#ifdef CSD_GEN_PREMIX
/* ---- pre-mixer injection: mixer builds only --------------------------
 *
 * Owner rule for the two csd_gen descriptors: exactly ONE thread may run
 * apply_pending()/fill() per frame. In a mixer build that thread is mixer1,
 * in a non-mixer build it is the audio thread; the caller selects at compile
 * time (CSD_GEN_PREMIX) and never both. Two owners would advance the NCO
 * twice per frame -- the tone comes out at exactly twice the commanded
 * frequency, which is the kind of wrong-but-sounding result a rig has to be
 * built specifically to catch.
 *
 * These statics belong to the mixer thread in that mode: only it reads or
 * writes them, so no handshake is needed. The one exception is
 * csd_gen_mix_want_fs, written by the audio thread as a single word
 * (XS3 word stores are atomic) and consumed here at the frame boundary --
 * scalar across the boundary, matching the rule the rest of the cross-tile
 * surface follows.
 */
/* Mixer-thread-private frame state: only mixer1 touches these, so no
 * hand-off machinery is needed. The two genuinely cross-thread words (stream
 * rate in, DoP verdict out) are NOT here -- they live in csd_gen_mailbox.c,
 * a C TU, because XC's parallel-usage checker will not let two branches of
 * lib_xua's single audio par share a variable. */
static unsigned csd_gen_mix_dsd;       /* last dsdMode the mixer recorded  */
static int      csd_gen_mix_votes;     /* frames toward DoP                */
static int      csd_gen_mix_leave;     /* frames toward PCM                */
static unsigned csd_gen_mix_fs;        /* rate this thread last applied    */
static unsigned csd_gen_mix_armed;     /* mode != OFF for this frame       */
static unsigned csd_gen_mix_valid;     /* wave holds this frame's samples  */
static int      csd_gen_mix_wave[2];   /* the frame's wave, L then R       */
/* Per-source Q15 gains for this frame (32768 = unity), copied from the
 * packed mailbox word in csd_gen_mix_begin -- mixer thread reads only. */
static unsigned csd_gen_mix_w_host = CSD_GEN_Q15_UNITY;
static unsigned csd_gen_mix_w_gen  = CSD_GEN_Q15_UNITY;

void csd_gen_mix_set_rate(unsigned fs_hz)
{
    /* Called by the audio thread at stream start. It may NOT call
     * csd_gen_notify_rate() directly in this mode: that edits csd_gen and
     * csd_gen_pend, which the mixer thread owns. Leaving a scalar in the
     * mailbox for the owner to collect keeps the single-writer property. */
    csd_mb_put_rate(fs_hz);
}

unsigned csd_gen_mix_dop(void)
{
    /* Read by the audio thread; written only by this thread above. */
    return csd_mb_get_dop();
}

void csd_gen_mix_begin(int hostL, int hostR, unsigned dsd_mode)
{
    /* Adopt first, then emit -- same boundary rule as csd_gen_inject(), so a
     * burst's first sample is the first sample of the wave. */
    csd_gen_apply_pending();

    /* Collect a stream-start rate the audio thread left for us. */
    unsigned want = csd_mb_get_rate();
    if (want != csd_gen_mix_fs && want != 0u) {
        csd_gen_mix_fs = want;
        csd_gen_notify_rate(want);
    }
    csd_gen_mix_dsd = dsd_mode;
    csd_gen_mix_armed = (csd_gen.mode != (unsigned)CSD_GEN_OFF);

    /* Per-source gains, read ONCE per frame into frame state so every
     * channel in this frame sees the same pair (the word is packed precisely
     * so the pair cannot split across the frame). */
    unsigned packed = csd_mb_get_gains();
    csd_gen_mix_w_host = (packed & 0xFFFFu);
    csd_gen_mix_w_gen  = (packed >> 16);

    /* DoP detection on the RAW host pair, BEFORE the sum. Once the tone is
     * added the marker bytes are gone, and lib_xua's own DoDsdDopCheck runs
     * downstream of us on that same buffer -- without this the stream would
     * never enter DoP while the generator is armed, and we would sum a tone
     * into what is really DSD payload. Detection therefore lives here in a
     * mixer build; its verdict reaches the DSP bypass via csd_gen_mix_dop().
     *
     * Voting, not per-frame truth: PCM carrying marker bytes in both channels
     * is ~2^-16 per frame, so 4 votes latch and 8 contrary votes release.
     * Err toward "is DoP": a false latch only bypasses the tone, a missed
     * latch corrupts DSD. */
    int match = CSD_GEN_DOP_MARK((unsigned)hostL) &&
                CSD_GEN_DOP_MARK((unsigned)hostR);
    /* The late-latch telemetry that used to sit here is REMOVED as attributed
     * (rig/dop_vote_probe.py: the 27,509-frame burst was ffmpeg startup
     * latency, match rate 1.000, self-converging). It was four cross-TU
     * accesses per frame on a hard real-time thread for a question that is
     * closed -- and the case-A ladder (factory +1.0 -> mixer+gen +11.7) says
     * per-frame work on this thread is not free. */
    if (match) {
        csd_gen_mix_leave = 0;
        if (csd_gen_mix_votes < 4 && ++csd_gen_mix_votes >= 4)
            csd_mb_put_dop(1u);
    } else {
        csd_gen_mix_votes = 0;
        if (csd_mb_get_dop() && ++csd_gen_mix_leave >= 8) {
            csd_mb_put_dop(0u);
            csd_gen_mix_leave = 0;
        }
    }

    /* The user gate: native DSD or DoP => the generator stays out of the data
     * path. It stays ARMED (SCPI still reports the wave) and resumes when the
     * host returns to plain PCM, with no re-arm. */
    /* The suppress paths MUST clear valid: csd_gen_mix_sample() sums
     * csd_gen_mix_wave whenever valid is set, so a stale wave left over from
     * a pre-latch frame would keep being ADDED to DSD payload even while the
     * counters report suppression. The rig caught this as emit+sup_dop
     * equalling the frame count with a long pre-latch window; without the
     * clear, "suppressed" meant only "the counters stopped", not "the data
     * path stopped". */
    if (!csd_gen_mix_armed) { csd_gen_mix_valid = 0u; return; }
    if (csd_gen_mix_dsd != 0u) { csd_gen_mix_valid = 0u; csd_gen_note(1u); return; }
    if (csd_mb_get_dop() != 0u) { csd_gen_mix_valid = 0u; csd_gen_note(2u); return; }

    csd_gen_fill(csd_gen_mix_wave);           /* advances the NCO exactly once */
    csd_gen_mix_valid = 1u;
    csd_gen_note(0u);
}

int csd_gen_mix_sample(unsigned ch, int host)
{
    /* Channels 0 and 1 get the per-source gain + summed tone; anything else
     * (SPDIF/ADAT host channels) passes through byte-exact. */
    if (ch > 1u) return host;

    /* DSD payload is NEVER scaled -- DoP markers live in these very bytes
     * and a MAC destroys them (the same hazard mixer.xc's own "we need all
     * 32bits for Native DSD" comment flags on the volume stage). Byte-exact
     * here is a data-integrity requirement, not a convenience: the host
     * stream under DoP IS the DSD bitstream. */
    if (csd_gen_mix_dsd != 0u || csd_mb_get_dop() != 0u) return host;

    if (!csd_gen_mix_valid) {
        /* Generator out of the path this frame; the host stream still gets
         * its individual gain, which is what makes the two controls
         * independent. Both-unity falls through to a byte-exact return. */
        if (csd_gen_mix_w_host == CSD_GEN_Q15_UNITY) return host;
        return csd_gen_mix_scale(host, csd_gen_mix_w_host);
    }
    return csd_gen_mix_gain_sum(host, csd_gen_mix_wave[ch],
                                csd_gen_mix_w_host, csd_gen_mix_w_gen);
}
#endif /* CSD_GEN_PREMIX */

/* Gate counters for SCPI (see the block above): the rig takes deltas across
 * a stream segment, so these are never reset and need no consistent triple. */
unsigned csd_gen_cnt_emit(void)   { return csd_gen_load(&csd_gen_n_emit); }
unsigned csd_gen_cnt_sup_dsd(void){ return csd_gen_load(&csd_gen_n_sup_dsd); }
unsigned csd_gen_cnt_sup_dop(void){ return csd_gen_load(&csd_gen_n_sup_dop); }
unsigned csd_gen_state_adopted(void)  { return csd_gen_load(&csd_gen_adopted); }
unsigned csd_gen_state_cmd(void)      { return csd_gen_load(&csd_gen_cmd); }
unsigned csd_gen_state_rejects(void)  { return csd_gen_load(&csd_gen_rejects); }
int      csd_gen_state_last_reject(void)
{
    return (int)csd_gen_load(&csd_gen_last_reject);
}

unsigned csd_gen_state_fs(void)       { return csd_gen.fs; }
unsigned csd_gen_state_f_a_q8(void)   { return csd_gen.f_a; }
unsigned csd_gen_state_f_b_q8(void)   { return csd_gen.f_b; }
unsigned csd_gen_state_amp(void)      { return csd_gen.amp; }
unsigned csd_gen_state_domain(void)   { return csd_gen.domain; }
unsigned csd_gen_state_tones(void)    { return csd_gen.tones; }

/* Stream rate changed: rebuild the frame-domain derivations so "1 Hz to
 * 20 kHz in 30 s" keeps meaning 30 s. Called from csd_dsp's rate hook, i.e.
 * the audio thread, so csd_gen is not being sampled concurrently.
 *
 * If the new rate makes the descriptor illegal -- 20 kHz is no longer under
 * Nyquist at 32 kHz, say -- the generator goes OFF rather than keep playing a
 * wave that folds. Silence is a visible failure; a quarter-octave-flat tone
 * is not. */
void csd_gen_notify_rate(unsigned fs_hz)
{
    if (csd_gen.mode == (unsigned)CSD_GEN_OFF) return;
    if (!csd_gen_fs_supported(fs_hz)) return;
    /* Keep the pending view consistent too, or a pending command adopted after
     * the rate change would resurrect the old rate. */
    if (csd_gen_set_rate(&csd_gen, fs_hz) != 0) {
        csd_gen.mode = (unsigned)CSD_GEN_OFF;
        csd_gen_reject(CSD_GEN_REJ_FS);
    }
    if (csd_gen_set_rate(&csd_gen_pend, fs_hz) != 0) {
        csd_gen_pend.mode = (unsigned)CSD_GEN_OFF;
    }
}
