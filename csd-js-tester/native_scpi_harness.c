/* Host harness for the REAL SCPI personality.
 *
 * Why this exists: the Python --sim backend in csd-js-tester is a
 * reimplementation of the device's behaviour, so it can pass 52/52 while the
 * real parser disagrees. Every question about command syntax -- does
 * "DIAG:DSP:PRESet 0" even match its pattern? does <string> require quotes? --
 * was being answered by guesswork against the Python model. This harness
 * compiles the actual csd_diag_scpi.c against the actual libscpi, so the
 * answers come from the same code that runs on the XU316.
 *
 * Only the device-side dependencies are stubbed (DSP state, codec registers),
 * and they are stubbed to RECORD what the personality asked for rather than to
 * return plausible values: that is the whole point, since the bug under
 * investigation is "the set command appears to succeed but the state never
 * changes".
 *
 * build: see build_native_scpi.sh
 */
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include "scpi/scpi.h"
#include "csd_diag.h"

/* ---- recorded device-side effects -------------------------------------- */
static int preset_set_calls;
static unsigned preset_set_value;
static unsigned preset_now = 2;              /* boot default under test */
static int reg_read_calls, reg_write_calls;
static unsigned last_reg, last_val;

void csd_dsp_set_preset(unsigned p)
{
    preset_set_calls++;
    preset_set_value = p;
    /* Apply it, as the real firmware does. A stub that only records would let
     * "PRESet 0 then PRESet? -> 2" pass as a bug forever: readback must come
     * from the same state the setter wrote, or the round-trip test tests
     * nothing. */
    preset_now = p;
}
unsigned csd_dsp_get_preset(void) { return preset_now; }
int csd_diag_reg_read(unsigned reg, unsigned *val) { reg_read_calls++; last_reg = reg; *val = 0x5a; return 1; }
int csd_diag_reg_write(unsigned reg, unsigned val) { reg_write_calls++; last_reg = reg; last_val = val; return 1; }
/* csd_diag_note_reg_write is defined in csd_diag_regs.xc, which is XC and not
 * compiled here, so it needs a stub. The reply/reg counters are NOT stubbed:
 * csd_diag_scpi.c defines those itself (they count its own ring behaviour) and
 * stubbing them is a duplicate symbol. */
void csd_diag_note_reg_write(void) { /* no-op: reg_write_calls counts the real call */ }

#include "csd_gen.h"           /* prototypes, so the stubs below are checked */

/* Identity snapshot, needed since csd_diag_scpi.c grew the DIAG:IDENTity:*
 * queries (commit 5acc597) -- this harness was not rebuilt at the time, which
 * is why the link only now fails. Filled with the material this board
 * actually reports, so *IDN? and the identity queries exercise their
 * formatting against plausible data rather than zeros: a snapshot of all
 * zeros makes "ready" false, and every query then answers with the
 * not-ready branch, so the harness would pass while never testing the
 * formatting at all. */
int csd_diag_identity_snapshot(csd_id_snapshot_t *snap)
{
    if (!snap) return 0;
    snap->ready = 1u;
    snap->stage = 6u;
    snap->source = 0u;
    snap->hash = 0x00000000201b0d69ull;      /* this board's committed serial */
    snap->words[0] = 0x8002u;
    snap->words[1] = 0x8002u;
    snap->words[2] = 0x6633u;
    snap->words[3] = 0x6633u;
    snprintf(snap->str, sizeof snap->str, "%s", "201b0d69");
    return 1;
}

/* ---- function generator stubs ------------------------------------------
 *
 * csd_gen.h is included (unlike the DSP/codec stubs above, whose headers drag
 * in xcore types) so the COMPILER checks these stubs against the real
 * prototypes. A stub whose signature drifted from the header would otherwise
 * link happily on the host and leave the personality calling something else
 * entirely -- the device build would then be the first to notice, on target.
 *
 * These APPLY what they are given, not merely record it, for the same reason
 * csd_dsp_set_preset above does: a stub that only records lets
 * "GENerator:SINe ... then GENerator:STATe? -> 0" sit unnoticed forever.
 * Readback must come from the state the setter wrote.
 *
 * The stub is NOT a model of the waveform code -- the host gate
 * (tools/dsp/test_gen.cpp) owns that, against the real core. This harness owns
 * the other half, the half the acoustic rig cannot see: does the SCPI pattern
 * match, do the parameters parse, does milliHz become the right Q8 value, and
 * does an out-of-range argument produce an error the host can read.
 */
static int gen_arm_calls, gen_tones_calls, gen_off_calls;
static int gen_fail;                       /* generator cases that disagreed  */
static unsigned gen_arm_mode, gen_arm_fs, gen_arm_fa, gen_arm_fb;
static unsigned gen_arm_domain, gen_arm_amp, gen_arm_period;
static unsigned gen_tones_count, gen_tones_amp, gen_tones_period;
static unsigned gen_tones_f[4];
static int gen_force_reject;           /* nonzero -> the next arm is refused  */
static int gen_last_reject_code;
static unsigned gen_reject_count;

/* Adopted state: what GENerator:STATe? must report, i.e. written by arm and
 * read back from here. Mirrors csd_gen.xc, where the getters read the
 * ADOPTED descriptor rather than the pending one. */
static unsigned gen_state_mode, gen_state_fs, gen_state_fa, gen_state_fb;
static unsigned gen_state_amp, gen_state_domain, gen_state_tones;
static unsigned gen_cmd, gen_adopted;

int csd_gen_request_arm(unsigned mode, unsigned fs, unsigned fa, unsigned fb,
                        unsigned domain, unsigned amp, unsigned period,
                        int *why)
{
    gen_arm_calls++;
    gen_arm_mode = mode; gen_arm_fs = fs; gen_arm_fa = fa; gen_arm_fb = fb;
    gen_arm_domain = domain; gen_arm_amp = amp; gen_arm_period = period;
    if (gen_force_reject) {
        if (why) *why = gen_last_reject_code;
        gen_reject_count++;
        return -1;
    }
    gen_state_mode = mode; gen_state_fs = fs;
    gen_state_fa = fa; gen_state_fb = fb ? fb : fa;   /* as the core does */
    gen_state_domain = domain; gen_state_amp = amp;
    gen_state_tones = 0;
    gen_cmd++; gen_adopted = gen_cmd;   /* harness has no audio thread */
    return 0;
}

int csd_gen_request_tones(unsigned fs, unsigned count, const unsigned *f,
                          unsigned amp, unsigned period, int *why)
{
    gen_tones_calls++;
    gen_tones_count = count; gen_tones_amp = amp; gen_tones_period = period;
    for (unsigned i = 0; i < count && i < 4; i++) gen_tones_f[i] = f[i];
    if (gen_force_reject) {
        if (why) *why = gen_last_reject_code;
        gen_reject_count++;
        return -1;
    }
    gen_state_mode = 4u;                 /* CSD_GEN_MODE_MULTITONE */
    gen_state_fs = fs; gen_state_amp = amp; gen_state_tones = count;
    gen_state_fa = f[0]; gen_state_fb = f[0];
    gen_cmd++; gen_adopted = gen_cmd;
    return 0;
}

void csd_gen_request_off(void)
{
    gen_off_calls++;
    gen_state_mode = 0u;
    gen_cmd++; gen_adopted = gen_cmd;
}

unsigned csd_gen_state_mode(void)   { return gen_state_mode; }
unsigned csd_gen_state_fs(void)     { return gen_state_fs; }
unsigned csd_gen_state_f_a_q8(void) { return gen_state_fa; }
unsigned csd_gen_state_f_b_q8(void) { return gen_state_fb; }
unsigned csd_gen_state_amp(void)    { return gen_state_amp; }
unsigned csd_gen_state_domain(void) { return gen_state_domain; }
unsigned csd_gen_state_tones(void)  { return gen_state_tones; }
unsigned csd_gen_state_cmd(void)    { return gen_cmd; }
unsigned csd_gen_state_adopted(void){ return gen_adopted; }
unsigned csd_gen_state_rejects(void){ return gen_reject_count; }
int      csd_gen_state_last_reject(void) { return gen_last_reject_code; }

/* ---- drive the personality exactly as the XC transport does ------------- */
static char out[1024];
static size_t out_len;

static void reset_capture(void) { out_len = 0; out[0] = 0; }

static int run(const char *cmd)
{
    extern void csd_scpi_begin(void);
    extern void csd_scpi_put(unsigned b);
    extern void csd_scpi_end(void);
    unsigned b;
    reset_capture();
    csd_scpi_begin();
    for (const char *p = cmd; *p; p++) csd_scpi_put((unsigned char)*p);
    csd_scpi_end();
    size_t n = 0;
    while ((b = csd_scpi_reply_take()) != 256u && n < sizeof(out) - 1)
        out[n++] = (char)b;
    out[n] = 0;
    return (int)n;
}

static void show(const char *label, const char *cmd)
{
    int n = run(cmd);
    printf("  %-34s -> %s  [%d bytes]\n", cmd,
           n ? out : "(NO REPLY)", n);
    if (label) printf("       %s\n", label);
}

/* Run one command with the error queue emptied first, then report the reply
 * AND the error that this command alone produced.
 *
 * Without the pre-drain this measurement is worthless: the queue accumulates
 * across the whole run, so a later command prints an earlier command's error
 * and looks like a new failure. That is exactly what happened -- REGister:READ?
 * appeared to produce -113 when in fact three errors were already pending and
 * it merely printed the oldest one.
 *
 * Reply + error together are the only reliable signal available, because the
 * callbacks are static in csd_diag_scpi.c and cannot be instrumented from
 * here: -113 means the pattern never matched, -224/-101 means it matched and
 * rejected the parameter, and a reply with no error means it worked. */
static void probe(const char *cmd)
{
    int n, e;
    char reply[512];
    do { e = run("SYSTem:ERRor:NEXT?"); } while (e > 0 && out[0] == '-');
    n = run(cmd);
    snprintf(reply, sizeof reply, "%s", n ? out : "");
    e = run("SYSTem:ERRor:NEXT?");
    printf("  %-30s reply=%-26s err=%s\n", cmd,
           n ? reply : "(none)", e ? out : "-");
}

/* ---- matcher experiments: what spelling does libscpi actually accept? ---
 *
 * Every command that takes a parameter returned -113 "Undefined header",
 * including the register commands, so the parameter SUGGESTS the pattern never
 * matched -- not the callback. In libscpi a SPACE separates compound-command
 * parts (SYSTem:ERRor NEXT?), so the parameter separator is the question.
 * SCPI_Match() is libscpi's own matcher, so this answers it from the same code
 * the device runs instead of from the Python sim's assumptions. */
static void probe_match(void)
{
    /* Header-only, the way findCommandHeader() really calls it. */
    struct { const char *pat, *cmd; } t[] = {
        {"DIAG:DSP:PRESet <string>",          "DIAG:DSP:PRESet"},
        {"DIAG:DSP:PRESet <string>",          "DIAG:DSP:PRESet 0"},
        {"DIAG:DSP:PRESet <numeric>",         "DIAG:DSP:PRESet"},
        /* Does a bare pattern also match the query form? Decides whether a
         * query entry and a setter entry can coexist, or whether the query
         * entry shadows the setter -- the comment in csd_diag_scpi.c claims
         * the matcher strips a trailing '?' from BOTH sides, which would make
         * a bare pattern match a query. Verify, do not assert. */
        {"DIAG:DSP:PRESet",                   "DIAG:DSP:PRESet"},
        {"DIAG:DSP:PRESet",                   "DIAG:DSP:PRESet?"},
        {"DIAG:DSP:PRESet?",                  "DIAG:DSP:PRESet"},
        {"DIAG:DSP:PRESet?",                  "DIAG:DSP:PRESet?"},
        {"DIAG:DSP:PRESet?",                  "DIAG:DSP:PRESet?7"},
        {"DIAG:DSP:PRESet?",                  "DIAG:DSP:PRESET?"},   /* case-insensitive? */
        {"REGister:READ?",                    "REGister:READ?"},
        {"REGister:WRITE",                    "REGister:WRITE"},
        /* optional-keyword style, from the library's own doc comment */
        {"[:MEASure]:VOLTage:DC?",            ":MEASure:VOLTage:DC?"},
    };
    printf("\n== SCPI_Match, header only (how findCommandHeader calls it) ==\n");
    for (size_t i = 0; i < sizeof t / sizeof t[0]; i++) {
        scpi_bool_t r = SCPI_Match(t[i].pat, t[i].cmd, strlen(t[i].cmd));
        printf("  pattern %-34s cmd %-24s -> %s\n", t[i].pat, t[i].cmd,
               r ? "MATCH" : "no match");
    }

    /* End-to-end through the real parser: register the pattern, feed the full
     * line, and see whether the callback fires. This is the only test that
     * exercises the same path the device takes. */
    printf("\n== end-to-end: which spellings reach the callback? ==\n");
    static const char *cmds[] = {
        "DIAG:DSP:PRESet 0", "DIAG:DSP:PRESet,0", "DIAG:DSP:PRESet 0,1",
        "DIAG:DSP:PRESet 0 1", "DIAG:DSP:PRESet \"0\"", "DIAG:DSP:PRESet?",
        "REGister:READ? 0x03", "REGister:READ?,0x03",
    };
    for (size_t i = 0; i < sizeof cmds / sizeof cmds[0]; i++) {
        int n, en, dn;
        preset_set_calls = 0; preset_set_value = 999;
        reg_read_calls = 0; reg_write_calls = 0;
        /* Drain first: the queue accumulates across the whole run, so an
         * undrained error from an earlier command is indistinguishable from
         * one caused by this command. run() returns a BYTE COUNT, so test the
         * captured text, not the return value indexed -- an earlier draft did
         * run(...)[0] and would have thrown. */
        do { dn = run("SYSTem:ERRor:NEXT?"); } while (dn > 0 && out[0] == '-');
        n = run(cmds[i]);
        printf("  %-26s reply=%-3s preset_calls=%d reg_calls=%d  %s",
               cmds[i], n ? "yes" : "NO ", preset_set_calls,
               reg_read_calls + reg_write_calls, n ? out : "");
        en = run("SYSTem:ERRor:NEXT?");
        printf("   err=%s\n", en ? out : "-");
    }
}

int main(void)
{
    probe_match();

    printf("== identity ==\n");
    show(NULL, "*IDN?");

    printf("\n== preset: does the SET parse and reach the device? ==\n");
    for (unsigned p = 0; p <= 2; p++) {
        char cmd[64];
        preset_set_calls = 0; preset_set_value = 999;
        snprintf(cmd, sizeof cmd, "DIAG:DSP:PRESet %u", p);
        show(NULL, cmd);
        printf("       csd_dsp_set_preset() called %d time(s), arg=%u\n",
               preset_set_calls, preset_set_value);
        snprintf(cmd, sizeof cmd, "DIAG:DSP:PRESet?");
        show(NULL, cmd);
    }

    printf("\n== quoted form, in case <string> requires quotes ==\n");
    preset_set_calls = 0;
    show(NULL, "DIAG:DSP:PRESet \"1\"");
    printf("       csd_dsp_set_preset() called %d time(s), arg=%u\n",
           preset_set_calls, preset_set_value);

    printf("\n== rejection paths ==\n");
    preset_set_calls = 0;
    show(NULL, "DIAG:DSP:PRESet 7");
    printf("       csd_dsp_set_preset() called %d time(s) (must be 0)\n", preset_set_calls);
    show(NULL, "SYSTem:ERRor:NEXT?");
    show(NULL, "SYSTem:ERRor:COUNt?");
    show(NULL, "BOGUS:CMD?");
    show(NULL, "SYSTem:ERRor:COUNt?");

    printf("\n== function generator: parse, units, and rejection ==\n");
    {
        /* Each case states the argument text and the argument the device must
         * have received. Comparing reply text alone would not catch a units
         * bug: "GENerator:SINe 48000,1000000,1073741824" answers OK whether
         * 1000000 became 1000 Hz or 1000 milliHz, and the difference is an
         * octave the rig then measures as a DUT fault. So every pass criterion
         * here is on the value the stub was handed. */
        static const struct {
            const char *cmd;
            int want_calls;             /* csd_gen_request_arm calls        */
            unsigned want_mode, want_fs, want_fa, want_fb;
            unsigned want_domain, want_amp, want_period;
            const char *note;
        } g[] = {
            /* 1 Hz .. 20 kHz, and the milliHz->Q8 rounding at both ends.
             * 256000 = 1000*256/1000 exactly; 5120000 = 20000*256. */
            {"GENerator:SINe 48000,1000,1073741824", 1, 1, 48000u, 256u, 0u,
             0u, 1073741824u, 0u, "1 Hz, hold (period absent)"},
            {"GENerator:SINe 48000,1000000,1073741824", 1, 1, 48000u, 256000u,
             0u, 0u, 1073741824u, 0u, "1 kHz, hold"},
            {"GENerator:SINe 48000,20000000,1073741824", 1, 1, 48000u,
             5120000u, 0u, 0u, 1073741824u, 0u, "20 kHz"},
            {"GENerator:SQUare 96000,1000000,536870912", 1, 2, 96000u,
             256000u, 0u, 0u, 536870912u, 0u, "square 1 kHz at 96 kHz"},
            {"GENerator:SINe 48000,1000000,1073741824,500", 1, 1, 48000u,
             256000u, 0u, 0u, 1073741824u, 500u, "500 ms burst"},
            {"GENerator:SWEep 48000,1000,20000000,LOG,1073741824,30000", 1,
             3, 48000u, 256u, 5120000u, 1u, 1073741824u, 30000u,
             "log sweep 1 Hz->20 kHz, 30 s"},
            {"GENerator:SWEep 48000,20000000,1000,LIN,1073741824,500", 1,
             3, 48000u, 5120000u, 256u, 0u, 1073741824u, 500u,
             "FALLING sweep: f_start > f_stop is legal, one code path"},
            {"GENerator:SWEep 48000,1000000,1000000,LOG,1073741824,1000", 1,
             3, 48000u, 256000u, 256000u, 1u, 1073741824u, 1000u,
             "STEADY sweep: f_start == f_stop"},
            {"GENerator:SWEep 48000,1000,20000000,lin,1073741824,500", 1,
             3, 48000u, 256u, 5120000u, 0u, 1073741824u, 500u,
             "lower-case LIN (device keyword match is case-insensitive)"},
            {"GENerator:NOISe 44100,1073741824,1000", 1, 5, 44100u,
             256u, 0u, 0u, 1073741824u, 1000u,
             "noise: f_a is a legal placeholder (the core requires >=1 Hz\n              for every mode), ignored by the noise branch"},
        };

        for (size_t i = 0; i < sizeof g / sizeof g[0]; i++) {
            gen_arm_calls = 0;
            gen_arm_mode = gen_arm_fs = gen_arm_fa = gen_arm_fb = 999999u;
            gen_arm_domain = gen_arm_amp = gen_arm_period = 999999u;
            int n = run(g[i].cmd);
            (void)n;
            int bad = (gen_arm_calls != g[i].want_calls) ||
                      (g[i].want_calls == 1 &&
                       (gen_arm_mode != g[i].want_mode ||
                        gen_arm_fs != g[i].want_fs ||
                        gen_arm_fa != g[i].want_fa ||
                        gen_arm_fb != g[i].want_fb ||
                        gen_arm_domain != g[i].want_domain ||
                        gen_arm_amp != g[i].want_amp ||
                        gen_arm_period != g[i].want_period));
            printf("  %-58s %s  calls=%d m=%u fs=%u fa=%u fb=%u dom=%u amp=%u per=%u\n",
                   g[i].cmd, bad ? "MISMATCH" : "ok",
                   gen_arm_calls, gen_arm_mode, gen_arm_fs, gen_arm_fa,
                   gen_arm_fb, gen_arm_domain, gen_arm_amp, gen_arm_period);
            if (g[i].note) printf("       %s\n", g[i].note);
            if (bad) {
                printf("       EXPECTED m=%u fs=%u fa=%u fb=%u dom=%u amp=%u per=%u\n",
                       g[i].want_mode, g[i].want_fs, g[i].want_fa,
                       g[i].want_fb, g[i].want_domain, g[i].want_amp,
                       g[i].want_period);
                gen_fail++;
            }
        }

        /* Noise passes no frequency, so fa/fb are whatever the caller used;
         * do not assert them. */

        /* Rejections: the command must NOT reach the device, and must leave an
         * error the host can read. Both halves matter -- a callback that
         * returns an error but has already armed the generator plays a wave
         * while telling the host the command failed. */
        static const struct { const char *cmd; const char *why; } bad[] = {
            {"GENerator:SINe 48000,999,1073741824",     "0.999 Hz: below 1 Hz"},
            {"GENerator:SINe 48000,20001000,1073741824", "20.001 kHz: above spec"},
            {"GENerator:SINe 48000,1000000,0",           "amplitude zero"},
            {"GENerator:SINe 48000,1000000,4294967295",  "amplitude above full scale"},
            {"GENerator:SWEep 48000,1000,20000000,LOG,1073741824,0", "sweep needs a period"},
            {"GENerator:SWEep 48000,1000,20000000,LOGLINEAR,1073741824,1000", "bad domain"},
            {"GENerator:TONEs 48000,1073741824,1000,1,1000000", "one tone is not a multitone"},
            {"GENerator:TONEs 48000,1073741824,1000,5,1000000,2000000,3000000,4000000,5000000", "five tones"},
        };
        for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
            gen_arm_calls = gen_tones_calls = 0;
            run("SYSTem:ERRor:COUNt?");              /* drain */
            probe(bad[i].cmd);
            int reached = gen_arm_calls + gen_tones_calls;
            char ecmd[64];
            snprintf(ecmd, sizeof ecmd, "SYSTem:ERRor:NEXT?");
            show(NULL, ecmd);
            printf("       %s -> device calls=%d (must be 0)\n",
                   bad[i].why, reached);
            if (reached != 0) { printf("       MISMATCH\n"); gen_fail++; }
        }
    }

    printf("\n== optional period: absent, present, and MALFORMED ==\n");
    /* The malformed case is the one that matters. libscpi's SCPI_Parameter
     * returns FALSE for both "no more input" and a bad separator, so a naive
     * "if (get optional) parse it" treats a mistyped burst length as "no
     * burst requested" and arms a steady tone in silence. The personality
     * distinguishes them with the error-queue delta; this checks it does. */
    {
        gen_arm_calls = 0; gen_arm_period = 12345u;
        probe("GENerator:SINe 48000,1000000,1073741824");
        printf("       absent -> calls=%d period=%u (must be 0)\n",
               gen_arm_calls, gen_arm_period);
        if (gen_arm_calls != 1 || gen_arm_period != 0u) gen_fail++;

        gen_arm_calls = 0; gen_arm_period = 12345u;
        probe("GENerator:SINe 48000,1000000,1073741824,250");
        printf("       present -> calls=%d period=%u (must be 250)\n",
               gen_arm_calls, gen_arm_period);
        if (gen_arm_calls != 1 || gen_arm_period != 250u) gen_fail++;

        gen_arm_calls = 0; gen_arm_period = 12345u;
        probe("GENerator:SINe 48000,1000000,1073741824,,");
        printf("       malformed trailing comma -> calls=%d "
               "(must be 0: not silently treated as absent)\n", gen_arm_calls);
        if (gen_arm_calls != 0) {
            printf("       MISMATCH: a malformed optional argument armed the "
                   "generator as a steady tone\n");
            gen_fail++;
        }
    }

    printf("\n== GENerator:STATe? reports what was armed ==\n");
    {
        probe("GENerator:OFF");
        probe("GENerator:SWEep 96000,20000,15000000,LOG,536870912,2000");
        show("mode=3 fs=96000 fa=5120 fb=3840000 dom=1 amp=536870912",
             "GENerator:STATe?");
        probe("GENerator:TONEs 48000,805306368,0,3,260000,1040000,4160000");
        show("mode=4 tones=3 amp=805306368", "GENerator:STATe?");
    }

    printf("\n== abbreviated mnemonics (libscpi <n> form) ==\n");
    {
        gen_arm_calls = 0;
        probe("GEN2:SIN 48000,1000000,1073741824");
        printf("       GEN2:SIN -> calls=%d (informational; not required)\n",
               gen_arm_calls);
    }

    printf("\n== registers (drained per-command: reply + own error) ==\n");
    reg_read_calls = 0;
    probe("REGister:READ? 0x03");
    printf("       csd_diag_reg_read() calls=%d reg=0x%02x\n", reg_read_calls, last_reg);
    reg_write_calls = 0;
    probe("REGister:WRITE 0x07,0x81");
    printf("       csd_diag_reg_write() calls=%d reg=0x%02x val=0x%02x\n",
           reg_write_calls, last_reg, last_val);
    probe("REGister:READ? 0xFF");           /* out of 8-bit range -> -224 expected */

    /* Why did "REGister:READ? 0x03" answer -113/-101 while "0xFF" returned
     * 0x5A? Same command, same pattern, only the parameter differs -- so this
     * is not the pattern bug. Spellings that separate 'the lexer rejects this
     * token' from 'this particular text is special': plain decimal, unpadded
     * hex, 0X upper prefix, quoted, and the failing value repeated (a repeat
     * that succeeds would mean state, not syntax). */
    printf("\n== parameter spellings (each drained, so errors are attributed) ==\n");
    {
        static const char *p[] = {
            "REGister:READ? 0x03", "REGister:READ? 0x03",  /* twice: state or syntax? */
            "REGister:READ? 3", "REGister:READ? 0x3", "REGister:READ? 0X03",
            "REGister:READ? \"0x03\"", "REGister:READ? ,0x03",
            "REGister:WRITE 0x07,0x81", "REGister:WRITE 0x7,0x81",
            "REGister:WRITE 0x07 0x81", "REGister:WRITE 7,129",
        };
        for (size_t i = 0; i < sizeof p / sizeof p[0]; i++) {
            reg_read_calls = 0; reg_write_calls = 0;
            probe(p[i]);
            printf("       device calls: read=%d write=%d\n",
                   reg_read_calls, reg_write_calls);
        }
    }

    /* ---- shipped syntax: base64 payload inside a quoted string -----------
     * Built with snprintf and a numeric quote constant so the source carries
     * no escaped quotes for an editing layer to mangle. These are the controls
     * for the diagnosis above: if every hex spelling fails and these succeed,
     * the lexer -- not the pattern, not the device -- is what killed the hex
     * form, which is why the protocol moved to base64 (BATMCU convention).
     *   Aw== -> [0x03]      Ayo= -> [0x03,0x2A]
     *   ""   -> empty        Aw   -> unpadded
     * Expected on the real device: the two well-formed payloads reach the
     * register callbacks (read=N / write=N), and the two malformed ones answer
     * -151 Invalid string data with ZERO device calls, because b64_decode
     * refuses before any I/O is attempted. */
    {
        static const struct { const char *cmd, *payload; } qp[] = {
            {"REGister:READ?",  "Aw=="},
            {"REGister:WRITE",  "Ayo="},
            {"REGister:READ?",  ""},
            {"REGister:READ?",  "Aw"},
        };
        char cmd[64];
        for (size_t i = 0; i < sizeof qp / sizeof qp[0]; i++) {
            snprintf(cmd, sizeof cmd, "%s %c%s%c", qp[i].cmd, 0x22,
                     qp[i].payload, 0x22);
            reg_read_calls = 0; reg_write_calls = 0;
            probe(cmd);
            printf("       device calls: read=%d write=%d\n",
                   reg_read_calls, reg_write_calls);
        }
    }
    /* Nonzero exit when a generator case disagreed, so build_native_scpi.sh
     * can gate on it. Only generator cases set gen_fail; the exploratory
     * register/preset output above stays informational. */
    if (gen_fail) printf("\nGENERATOR SCPI: %d MISMATCH(ES)\n", gen_fail);
    else          printf("\nGENERATOR SCPI: PASS\n");
    return gen_fail ? 1 : 0;
}
