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
static unsigned          s_phase;        /* calls since the last input consume */
static unsigned          s_in_hz;        /* host stream rate */
static unsigned          s_out_hz;       /* confirmed I2S/codec rate, 0 = not armed */
static unsigned          s_bypass;       /* 1 => pass frames through untouched */
static unsigned          s_frames;       /* diagnostic */

unsigned csd_ups_diag_state(void) { return (s_out_hz == 384000u && !s_bypass) ? 1u : 0u; }
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
        s_q_wr[c] = 0u; s_q_rd[c] = 0u;
    }
    s_phase = 0u;
}

void csd_ups_set_input_rate(unsigned hz)
{
    /* A rate change is a stream restart: history from the previous rate is
     * invalid, so it is dropped rather than filtered. */
    if (hz != s_in_hz) { s_in_hz = hz; csd_ups_reset(); }
}

void csd_ups_arm(unsigned out_hz)
{
    /* Called by the board code once it has actually configured the codec and the
     * I2S clocks. Anything other than 384 kHz disables the path outright. */
    if (out_hz != 384000u)      { s_out_hz = 0u;       s_bypass = 1u; csd_ups_reset(); }
    else if (s_out_hz != 384000u) { s_out_hz = 384000u; s_bypass = 0u; csd_ups_reset(); }
}

/* One frame: s[0]=L s[1]=R, both Q31. Returns non-zero when it wrote them, zero
 * when it left the frame alone (disarmed or unsupported rate). */
int csd_ups_frame(unsigned CSD_UPS_CPTR s)   /* expands to plain '*' in C */
{
    const unsigned ratio = csd_up_ratio(s_in_hz);

    if (s_bypass || s_out_hz != 384000u || ratio == 0u) return 0;   /* untouched */

    if (s_phase == 0u) {
        /* Pull one host frame and expand it into the per-channel queues. */
        int32_t lin = (int32_t)s[0], rin = (int32_t)s[1];
        int32_t lo[8], ro[8];
        int32_t ta0[8], tb0[8], ta1[8], tb1[8];
        csd_cascade_process(&s_casc[0], s_in_hz, &lin, 1u, lo, ta0, tb0);
        csd_cascade_process(&s_casc[1], s_in_hz, &rin, 1u, ro, ta1, tb1);
        for (unsigned i = 0; i < ratio; i++) {
            s_q[0][s_q_wr[0]] = lo[i]; s_q_wr[0] = (s_q_wr[0] + 1u) & 7u;
            s_q[1][s_q_wr[1]] = ro[i]; s_q_wr[1] = (s_q_wr[1] + 1u) & 7u;
        }
    }

    /* Hand out one 384 kHz frame, then band-limit it. */
    for (unsigned c = 0; c < 2u; c++) {
        int32_t vin  = s_q[c][s_q_rd[c]];
        int32_t vout = 0;
        s_q_rd[c] = (s_q_rd[c] + 1u) & 7u;
        csd_bl_process(&s_bl[c], &vin, 1u, &vout);
        s[c] = (unsigned)vout;
    }

    s_phase++;
    if (s_phase >= ratio) s_phase = 0u;
    s_frames++;
    return 1;
}
