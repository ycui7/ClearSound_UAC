/* csd_power_state implementation -- see the header for the tile rule, which is
 * the whole point of this file. Every function is one store or one load of one
 * volatile word (one aligned 32-bit word cannot tear, and there is no
 * read-modify-write to race), except the trim decision at the bottom.
 */
#include "csd_power_state.h"
#include "csd_gen.h"
#include "csd_power.h"      /* the platform half of the idle-power contract */

/* ------------------------------ TILE[1]: stream witness -------------------- */

static volatile unsigned pw_host_activity = 0u;
static volatile unsigned pw_host_frames   = 0u;

void csd_pw_note_host_sample(unsigned ch, int sample)
{
    if (ch >= 2u) return;                 /* output-only board; defensive     */
    pw_host_frames++;
    if (sample != 0 && (unsigned)sample != CSD_PW_UNDERFLOW_WORD) {
        pw_host_activity++;
    }
}
unsigned csd_pw_host_activity(void) { return pw_host_activity; }

unsigned csd_pw_host_frames(void)   { return pw_host_frames; }

/* csd_power's witness: "has real audio arrived since the last ask?". Sits with
 * the counters it reads, so the cursor cannot drift from the thing counted.
 *
 * Edge-consuming by contract, which is what lets a teardown ask "did audio
 * return while we were busy?" and get a truthful answer rather than "audio was
 * playing at some point". Note it reads the SAME counter the trace prints, so
 * the print and the decision cannot disagree.
 *
 * What counts as real is decided by csd_pw_note_host_sample: non-zero, and not
 * the buffer path's underfill word. An LRCLK that toggles with a silent bus is
 * not a stream -- measured with the box quiet, this counter sat frozen at
 * 868140 while pw_host_frames climbed to 12.4 M in the same 10 s window. */
unsigned csd_pw_audio_arrived(void)
{
    static unsigned cursor = 0u;
    unsigned a = pw_host_activity;
    if (a == cursor) return 0u;
    cursor = a;
    return 1u;
}

/* No word census here. A previous cut exposed min/max/last/distinct getters
 * that NOTHING ever wrote -- csd_pw_note_host_sample does not touch them -- so
 * every 'PWTRACE words min=0x...' line the bench printed was a pair of
 * initialisers dressed as a measurement. Deleting them rather than completing
 * them: act-vs-frames already separates 'streaming' from 'clock alive, bus
 * silent', which is the only question that was ever answered from them. If a
 * real census is wanted, it must be written in note_host_sample and proven to
 * change on a known-good tone before anyone quotes it. */

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
