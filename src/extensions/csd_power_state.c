/* csd_power_state implementation -- see the header for the tile rule, which is
 * the whole point of this file. Every function is one store or one load of one
 * volatile word (one aligned 32-bit word cannot tear, and there is no
 * read-modify-write to race), except the trim decision at the bottom.
 */
#include "csd_power_state.h"
#include "csd_gen.h"

/* ------------------------------ TILE[1]: stream witness -------------------- */

static volatile unsigned pw_host_activity = 0u;
static volatile unsigned pw_host_frames   = 0u;

/* DIAGNOSTIC (temporary): what are the words the witness keeps counting when
 * nothing is playing? min/max/last + how many DISTINCT values, so the bench
 * can tell a constant fill word from a live stream from stale buffer content
 * without a logic analyser. */
static volatile unsigned pw_dbg_min = 0xFFFFFFFFu, pw_dbg_max = 0u, pw_dbg_last = 0u,
                         pw_dbg_distinct = 0u, pw_dbg_prev = 0xFFFFFFFFu;

void csd_pw_note_host_sample(unsigned ch, int sample)
{
    if (ch >= 2u) return;                 /* output-only board; defensive     */
    pw_host_frames++;
    if (sample != 0 && (unsigned)sample != CSD_PW_UNDERFLOW_WORD) {
        pw_host_activity++;
    }
}
unsigned csd_pw_host_activity(void) { return pw_host_activity; }

void csd_pw_dbg_reset(void)
{
    pw_dbg_min = 0xFFFFFFFFu; pw_dbg_max = 0u; pw_dbg_last = 0u;
    pw_dbg_distinct = 0u; pw_dbg_prev = 0xFFFFFFFFu;
}
unsigned csd_pw_dbg_word(unsigned which)
{
    switch (which) {
        case 0u: return pw_dbg_min;
        case 1u: return pw_dbg_max;
        case 2u: return pw_dbg_last;
        default: return pw_dbg_distinct;
    }
}
unsigned csd_pw_host_frames(void)   { return pw_host_frames; }

/* ------------------------------ TILE[1]: DSP bypass ------------------------ */

static volatile unsigned pw_dsp_bypass = 0u;
void     csd_pw_request_dsp_bypass(unsigned enable) { pw_dsp_bypass = enable ? 1u : 0u; }
unsigned csd_pw_dsp_bypass_requested(void)          { return pw_dsp_bypass; }

static volatile unsigned pw_saved_preset = CSD_PW_PRESET_NONE;
void csd_pw_save_preset(unsigned p)
{
    if (pw_saved_preset == CSD_PW_PRESET_NONE) pw_saved_preset = p;
}
unsigned csd_pw_take_saved_preset(void)
{
    unsigned s = pw_saved_preset;
    pw_saved_preset = CSD_PW_PRESET_NONE;
    return s;
}

/* ------------------------------ TILE[1]: last applied rate ----------------- */

static volatile unsigned pw_last_mclk = 0u;
void     csd_pw_note_rate(unsigned mclk) { pw_last_mclk = mclk; }
unsigned csd_pw_last_mclk(void)          { return pw_last_mclk; }

/* ------------------------------ TILE[1]: boot trim cache ------------------- */

static unsigned pw_trim_last  = 0u;
static unsigned pw_trim_valid = 0u;

unsigned csd_pw_trim_boot_should_write(unsigned q31)
{
    /* Outer gate: valid cache and nothing marked dirty means nothing to do.
     * The dirty flag is set by the SCPI trim setter, so a value change still
     * reaches the codec when the Q31 word itself is unchanged. */
    if (pw_trim_valid && !csd_mb_get_trim_dirty()) return 0u;
    unsigned write = 0u;
    if (!pw_trim_valid || pw_trim_last != q31) {
        pw_trim_last  = q31;
        pw_trim_valid = 1u;
        write = 1u;
    }
    csd_mb_clear_trim_dirty();
    return write;
}
