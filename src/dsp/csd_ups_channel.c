/* CSD upsampler channel -- the per-frame glue between the audio thread and the
 * portable core (src/dsp/csd_upsample.c).
 *
 * WHY THIS FILE IS C AND NOT XC
 * The core's state structs (csd_cascade_state, csd_bl_state) are typed
 * separately in each language, and xcc's cross-language link check rejects the
 * pair outright:
 *     Type of symbol `csd_bl_reset' has mismatch with previous definition
 * So the structs must never appear in a signature that an XC translation unit
 * sees. Keeping the state here, in C, means the audio thread crosses into this
 * module only through csd_ups_frame(unsigned *) and the four control calls --
 * plain int/unsigned, which both languages agree on.
 *
 * It also suits the intended architecture: the upsampler is the DSP for this
 * build, so it owns the frame and the state that goes with it.
 *
 * ARMING RULE. This path is only valid when the I2S output clock and the codec
 * both run at 384 kHz. If they run at the host rate, emitting upsampled words
 * into a 48 kHz LRCLK is not "lower quality", it is wrong by a factor of 8, so
 * the module refuses to run and leaves the frame untouched until
 * csd_ups_arm() is called with the real output rate. Fail loud, never silent.
 *
 * Rate handling: stage A consumes one host frame every `ratio` calls (8/4/2)
 * and produces `ratio` outputs per input; a small queue hands them out one
 * frame at a time. Stage B then runs on every output sample at 384 kHz.
 */
#include "csd_upsample_hook.h"
#include "csd_upsample.h"

static csd_cascade_state s_casc[2];
static csd_bl_state      s_bl[2];
static int32_t           s_q[2][8];      /* expanded samples awaiting delivery */
static unsigned          s_q_wr[2], s_q_rd[2];
static unsigned          s_occ[2];       /* words in each ring, push++ pop-- */
static int32_t           s_last[2];      /* last emitted word, underrun hold */
static unsigned          s_in_hz;        /* host stream rate */
static unsigned          s_out_hz;       /* confirmed I2S/codec rate, 0 = not armed */
static unsigned          s_bypass;       /* 1 => pass frames through untouched */
static unsigned          s_frames;       /* diagnostic */

/* Runtime enable for the whole option. DEFAULT OFF: shipping behaviour keeps
 * the DAC's built-in filter, which is the acoustically proven path. The MIDI
 * surface is the intended way to turn it on.
 *
 * Enabled does not mean active. The path still has to be armed at a family
 * output rate (csd_ups_arm) or the frame is left alone and refusals count. */
static unsigned s_enabled = 0u;
static unsigned s_refusals = 0u;

/* The operator's INTENT, kept on the tile that reads it. Deliberately separate
 * from s_enabled: enabled is what the codec ACCEPTED, want is what the journal
 * says. Reading the persisted flag directly here would be a cross-tile static
 * read -- the tile[0] store writes it in csd_trim_boot_load(), which is not
 * guaranteed to have run before the first stream, and this file's own history
 * has that exact bug (the trim journal journalled zeros while seq climbed).
 * tile[1] fetches it over the channel at stream start instead. */
static volatile unsigned s_want = 0u;
void     csd_ups_set_want(unsigned w) { s_want = w ? 1u : 0u; }
unsigned csd_ups_get_want(void)       { return s_want; }

void csd_ups_set_enabled(unsigned enable)   /* not 'on': reserved in XC */
{
    const unsigned now = enable ? 1u : 0u;
    if (now != s_enabled) {
        s_enabled = now;
        csd_ups_reset();          /* a mode switch is a stream restart */
    }
}
unsigned csd_ups_is_enabled(void) { return s_enabled; }
unsigned csd_ups_diag_refusals(void) { return s_refusals; }

unsigned csd_ups_diag_state(void) { return (s_enabled && s_out_hz != 0u && !s_bypass) ? 1u : 0u; }
unsigned csd_ups_diag_frames(void) { return s_frames; }
unsigned csd_ups_diag_in_hz(void)  { return s_in_hz; }
unsigned csd_ups_diag_out_hz(void) { return s_out_hz; }

void csd_ups_reset(void)
{
    csd_cascade_reset(&s_casc[0]);
    csd_cascade_reset(&s_casc[1]);
    csd_bl_reset(&s_bl[0]);
    csd_bl_reset(&s_bl[1]);
    for (unsigned c = 0; c < 2u; c++) {
        for (unsigned i = 0; i < 8u; i++) s_q[c][i] = 0;
        s_q_wr[c] = 0u; s_q_rd[c] = 0u; s_occ[c] = 0u;
        s_last[c] = 0;
    }
}

void csd_ups_set_input_rate(unsigned hz)
{
    /* A rate change is a stream restart: history from the previous rate is
     * invalid, so it is dropped rather than filtered. */
    if (hz != s_in_hz) { s_in_hz = hz; csd_ups_reset(); }
}

unsigned csd_ups_expected_out_hz(unsigned in_hz)
{
    /* One source of truth for the family mapping. The board code calls this to
     * choose MCLK; this module calls it to validate arming. 384000 for the 48
     * series, 352800 for the 44.1 series, 0 for anything else. */
    return (unsigned)csd_up_out_fs((uint32_t)in_hz);
}

void csd_ups_arm(unsigned out_hz)
{
    /* Called by the board code once it has actually configured the codec and the
     * I2S clocks. Two families are acceptable -- 384 kHz and 352.8 kHz -- but the
     * value must belong to the family the input rate is in. Arming 44.1 kHz
     * material at 384 kHz would be a ratio of 8.708, not a filter setting: the
     * path is disabled rather than run at a wrong rate. */
    const unsigned want = csd_ups_expected_out_hz(s_in_hz);
    if (out_hz == 0u || (want != 0u && out_hz != want) ||
        (want == 0u && out_hz != 384000u && out_hz != 352800u)) {
        s_out_hz = 0u; s_bypass = 1u; csd_ups_reset();
    } else if (s_out_hz != out_hz) {
        s_out_hz = out_hz; s_bypass = 0u; csd_ups_reset();
    }
}

unsigned csd_ups_host_active(void)
{
    /* Same predicate the interlock uses: enabled (codec accepted the OSF
     * bypass), armed (wire really at the family rate), not bypassing. */
    return (s_enabled && s_out_hz != 0u && !s_bypass) ? 1u : 0u;
}

/* One host pair in, at host-pair cadence (called from UserBufferManagement).
 * Stage A only: the wire words come out of csd_ups_slot at LRCLK cadence.
 * Balance proof: push `ratio` words per channel per pair; the audiohub loop
 * pops exactly one word per channel per LRCLK, and one pair arrives every
 * `ratio` LRCLKs -- occupancy returns to zero right before the next push. */
void csd_ups_host_pair(int l, int r)
{
    const unsigned ratio = csd_up_ratio(s_in_hz);

    if (!s_enabled) return;               /* option off: nothing to queue */
    if (s_bypass || s_out_hz == 0u || ratio == 0u) {
        s_refusals++;                     /* on but refused: count, no queue */
        return;
    }

    int32_t lin = (int32_t)l, rin = (int32_t)r;
    int32_t lo[8], ro[8];
    int32_t ta0[8], tb0[8], ta1[8], tb1[8];
    csd_cascade_process(&s_casc[0], s_in_hz, &lin, 1u, lo, ta0, tb0);
    csd_cascade_process(&s_casc[1], s_in_hz, &rin, 1u, ro, ta1, tb1);
    for (unsigned i = 0; i < ratio; i++) {
        s_q[0][s_q_wr[0]] = lo[i]; s_q_wr[0] = (s_q_wr[0] + 1u) & 7u;
        s_q[1][s_q_wr[1]] = ro[i]; s_q_wr[1] = (s_q_wr[1] + 1u) & 7u;
        s_occ[0]++; s_occ[1]++;
    }
    s_frames++;
}

/* One wire slot out, at LRCLK cadence (called from the audiohub loop, one
 * channel per slot). Empty ring means the host stalled: hold the last word
 * rather than emit stale ring memory -- an honest repeat, never a lie. */
int csd_ups_slot(unsigned ch, int w)
{
    if (!csd_ups_host_active()) return w; /* not active: word untouched */
    if (ch >= 2u)          return w;      /* not our channel */

    if (s_occ[ch] == 0u) {
        int32_t out = s_last[ch];
        csd_bl_process(&s_bl[ch], &out, 0u, &out);  /* 0: state untouched */
        return (int)s_last[ch];
    }
    int32_t vin  = s_q[ch][s_q_rd[ch]];
    int32_t vout = 0;
    s_q_rd[ch]   = (s_q_rd[ch] + 1u) & 7u;
    s_occ[ch]--;
    csd_bl_process(&s_bl[ch], &vin, 1u, &vout);
    s_last[ch]   = vout;
    return (int)vout;
}
