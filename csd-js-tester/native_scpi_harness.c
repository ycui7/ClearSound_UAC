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
    return 0;
}
