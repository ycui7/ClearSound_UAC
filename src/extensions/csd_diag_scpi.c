/* SCPI personality for the ClearSound DAC diagnostic interface.
 *
 * Scope is deliberately minimal (owner directive 2026-10-30): identity, the
 * error queue, DSP preset, and raw codec register read/write. Nothing else is
 * registered. An unregistered command makes the parser answer -113 "Undefined
 * header", which is the truthful SCPI response; a registered-but-ignored
 * command would let a host believe something we never did. So MEASure:*,
 * SOURce:* and CONFigure:* are absent by design, not forgotten.
 *
 * No floats: every reply is built by our own integer formatter and emitted
 * with SCPI_ResultCharacters(), so the soft-float routines stay unreferenced.
 */
#include <stdint.h>
#include <string.h>

/* No <stdio.h> on purpose -- see fmt_u32 / fmt_u32_hex. */

#include "scpi/scpi.h"
#include "csd_diag.h"
#include "csd_dsp.h"
#include "csd_version.h"
#include "csd_identity.h"
#include "csd_gen.h"
/* L/R trim persistence (TODO 3) is reached through csd_diag.h's proxy calls,
 * never by including csd_trim_store.h: that header's entry points drive the
 * tile[0] SQI ports and may only be called by the tile[0] codec server. */

#define CSD_IDN_VENDOR   "ClearSound"
#define CSD_IDN_MODEL    "CSD-22"
/* Serial is not a constant any more, so it is not passed through SCPI_Init
 * at all: q_idn reads it live. See q_idn for why capture-at-init would be
 * wrong. This macro only fills the now-unused idn3 slot. */
#define CSD_IDN_SERIAL   ""
#define CSD_IDN_FIRMWARE CSD_FW_VERSION

/* ---- output plumbing -------------------------------------------------- */
#define REPLY_MAX 320

static unsigned reply_len;      /* bytes emitted by the current command */
static unsigned reply_truncs;

/* Reply ring, owned here (next to the parser) and pulled byte-by-byte by the
 * XC transport. Overflow drops and counts -- it never blocks, because the
 * consumer is a core that must keep answering the USB endpoint. */
#define REPLY_BYTES 512
static unsigned char rbuf[REPLY_BYTES];
static unsigned      rwr, rrd;
static unsigned      reply_drops;

static void ring_put(unsigned char b)
{
    unsigned nxt = (rwr + 1u) % REPLY_BYTES;
    if (nxt == rrd) { reply_drops++; return; }
    rbuf[rwr] = b;
    rwr = nxt;
}

unsigned csd_scpi_reply_ready(void) { return rwr != rrd; }

unsigned csd_scpi_reply_take(void)
{
    if (rwr == rrd) return 256u;              /* 256 = "empty", never a byte */
    unsigned b = rbuf[rrd];
    rrd = (rrd + 1u) % REPLY_BYTES;
    return b;
}

SCPI_FPTR_IFACE size_t SCPI_Write(scpi_t *context, const char *data, size_t len)
{
    (void)context;
    /* The parser may emit more than one line for some commands; a per-reply
     * cap keeps one command from monopolising the ring. Shortening is counted,
     * so the host can tell a truncated answer from a complete one. */
    if (reply_len + len > REPLY_MAX) {
        len = (reply_len < REPLY_MAX) ? (REPLY_MAX - reply_len) : 0;
        reply_truncs++;
    }
    for (size_t i = 0; i < len; i++) ring_put((unsigned char)data[i]);
    reply_len += (unsigned)len;
    return len;
}

SCPI_FPTR_IFACE int SCPI_Error(scpi_t *context, int_fast16_t err)
{
    /* Required interface member. Errors reach the host through the SCPI error
     * queue (SYSTem:ERRor:NEXT?), the standard channel, so there is nothing to
     * do on a side band. Keeping the symbol is mandatory, not decorative. */
    (void)context; (void)err;
    return 0;
}

SCPI_FPTR_IFACE scpi_result_t SCPI_Flush(scpi_t *context) { (void)context; return SCPI_RES_OK; }
SCPI_FPTR_IFACE scpi_result_t SCPI_Reset(scpi_t *context) { (void)context; return SCPI_RES_OK; }

SCPI_FPTR_IFACE scpi_result_t SCPI_Control(scpi_t *context, scpi_ctrl_name_t ctrl, scpi_reg_val_t val)
{
    (void)context; (void)ctrl; (void)val;
    return SCPI_RES_OK;
}

/* ---- integer formatting we own ----------------------------------------
 * sprintf is banned here for two independent reasons, and the first is the one
 * that actually bit us: the xcore printf family is RECURSIVE, and a recursive
 * function anywhere in a tile's call graph makes the link-time stack analyser
 * refuse the whole tile --
 *     Error: Meta information ("q_dsp.nstackwords") for function "q_dsp"
 *            cannot be determined.
 *     Error: lower bound could not be calculated (function is recursive?).
 * Second, printf costs kilobytes of flash and, on the audio threads, the
 * xscope backpressure that cost us the silent-DAC week.
 * Two forms cover every reply we ever send, so we write them ourselves. */

/* Decimal, no leading zeros. NUL-terminates, returns the digit count. */
static unsigned fmt_u32(uint32_t v, char *out, unsigned cap)
{
    char tmp[10];
    unsigned n = 0, i = 0;

    do { tmp[n++] = (char)('0' + (v % 10u)); v /= 10u; } while (v && n < sizeof tmp);
    if (n > cap) { out[0] = 0; return 0; }        /* callers size generously */
    while (n) out[i++] = tmp[--n];
    out[i] = 0;
    return i;
}

/* "0x" then uppercase hex, minimal digits. NUL-terminates, returns length. */
static unsigned fmt_u32_hex(uint32_t v, char *out, unsigned cap)
{
    static const char digits[] = "0123456789ABCDEF";
    char tmp[8];
    unsigned n = 0, i = 0;

    do { tmp[n++] = digits[v & 0xFu]; v >>= 4; } while (v && n < sizeof tmp);
    if (n + 2u > cap) { out[0] = 0; return 0; }
    out[i++] = '0'; out[i++] = 'x';
    while (n) out[i++] = tmp[--n];
    out[i] = 0;
    return i;
}

/* ---- number parsing we own -------------------------------------------- */
/* Register addresses and values are conventionally written in hex. Whether
 * this library's numeric path accepts a 0x prefix is not documented, so we do
 * not depend on it: the parameter is taken as text and parsed here, accepting
 * 0x1f, 1Fh, 1f and decimal 31 alike. A bare number is decimal -- the
 * alternative (bare = hex) would silently reinterpret every plain integer. */
static int parse_num(const char *s, size_t len, uint32_t *out)
{
    while (len && (*s == ' ' || *s == '\t')) { s++; len--; }
    if (!len) return 0;

    unsigned base = 10;
    if (len >= 2 && s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) { s += 2; len -= 2; base = 16; }
    else if (len >= 1 && (s[len - 1] == 'h' || s[len - 1] == 'H')) { len--; base = 16; }
    if (!len) return 0;

    uint64_t v = 0;
    for (size_t i = 0; i < len; i++) {
        unsigned d;
        char c = s[i];
        if (c >= '0' && c <= '9')                        d = (unsigned)(c - '0');
        else if (base == 16 && c >= 'a' && c <= 'f')     d = (unsigned)(c - 'a') + 10u;
        else if (base == 16 && c >= 'A' && c <= 'F')     d = (unsigned)(c - 'A') + 10u;
        else return 0;
        if (d >= base) return 0;
        v = v * base + d;
        if (v > 0xFFFFFFFFull) return 0;                 /* reject, never wrap */
    }
    *out = (uint32_t)v;
    return 1;
}

/* Signed sibling of parse_num: '-256' is the whole point for the trim
 * commands, and parse_num's digit classifier rejects '-' by design (a
 * wrapped -1 reading as 0xFFFFFFFF is the failure mode it prevents).
 * Same bases, same reject-never-wrap rule; range checks belong to the
 * caller, which knows its contract. */
static int parse_snum(const char *s, size_t len, int32_t *out)
{
    int neg = 0;
    while (len && (*s == ' ' || *s == '\t')) { s++; len--; }
    if (!len) return 0;
    if (*s == '-') { neg = 1; s++; len--; }
    else if (*s == '+') { s++; len--; }
    uint32_t u;
    if (!parse_num(s, len, &u)) return 0;
    if (neg) {
        if (u > 0x80000000u) return 0;           /* cannot represent INT32_MIN+1.. */
        *out = -(int32_t)u;
    } else {
        if (u > 0x7FFFFFFFu) return 0;
        *out = (int32_t)u;
    }
    return 1;
}

/* One parameter as text, copied into our own buffer. SCPI_ParamCharacters
 * returns a pointer into the parse buffer -- valid during the callback -- but
 * copying keeps the argument handling obvious and immune to a later refactor. */
static int param_text(scpi_t *context, char *buf, size_t cap, scpi_bool_t mandatory)
{
    const char *p;
    size_t len = 0;
    if (!SCPI_ParamCharacters(context, &p, &len, mandatory)) return 0;
    if (len >= cap) len = cap - 1;
    memcpy(buf, p, len);
    buf[len] = 0;
    return 1;
}

/* Report a codec-access failure (or any "well formed but we could not do it").
 *
 * Why -200 and not -300: the IEEE 488.2 hardware-error codes (-300 group,
 * SCPI_ERROR_HARDWARE_ERROR) live only in the library's FULL error list, and
 * this build uses the minimal one (USE_FULL_ERROR_LIST=0 in
 * scpi_user_config.h) to keep the error table out of flash. -200 Execution
 * error is in the minimal set and is truthful here.
 *
 * No free-form reason string, deliberately. That is the job of
 * SCPI_ErrorPushEx's `info` argument, which only exists when
 * USE_DEVICE_DEPENDENT_ERROR_INFORMATION is on -- and that path routes through
 * the library's side "heap" (scpiheap_*), which the xcore link-time stack
 * analyser refuses to bound, so the task cannot be linked at all. Carrying a
 * sentence is not worth an unbuildable image: the host learns WHICH command
 * failed and gets an error CODE, and the read/write paths below differ by
 * command anyway. */
static void push_hw_error(scpi_t *context)
{
    SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
}

/* ---- base64: the wire format for raw register payloads -----------------
 *
 * Adopted from the proven BATMCU precedent (specialtycircuits repo,
 * stm32core/BATMCU_R8_F401RCT6/.dsh/skills/batmcu-spi-bringup/SKILL.md):
 *
 *   DIAGnostic:SPI0:CS1:WRB64? "<base64>"   # write+read, same length
 *   DIAGnostic:SPI0:CS1:WB64   "<base64>"   # write only
 *
 * Two reasons this is the right shape here, both measured on this device
 * rather than assumed:
 *
 * 1. libscpi's lexer rejects several natural spellings of a hex byte before
 *    the callback ever runs. Observed through the native harness, same
 *    command with only the parameter varied:
 *      REGister:READ? 0xFF        -> reply 0x5A          (works)
 *      REGister:READ? 0x3         -> reply 0x5A          (works)
 *      REGister:READ? 0x03        -> -101 Invalid character
 *      REGister:READ? 0X03        -> -101 Invalid character
 *      REGister:WRITE 7,129       -> accepted
 *      REGister:WRITE 0x07,0x81   -> -101 Invalid character
 *    A raw-payload command whose syntax depends on how many digits the value
 *    happens to have is unusable as a bench instrument. Base64 removes the
 *    class: the payload is one opaque token, and quoting is the form the
 *    lexer handles uniformly.
 *
 * 2. It matches what the host already knows how to do. The batmcu harness
 *    pattern is proven (scpi_tester/dac_definitive.py): encode the frame,
 *    send, decode the LAST quoted string in the reply. The trap recorded
 *    there is reproduced on purpose in the client: a reply can carry more
 *    than one quoted string, and base64 of the literal "ACK" decodes to
 *    0x00 0x22 -- a ghost payload that looks like a real register read of 0.
 *    So the device emits exactly ONE quoted string and the client still takes
 *    the last, so neither side can be fooled by an echo.
 *
 * Strict decoder: canonical alphabet only, no whitespace, padding required.
 * The host is our own client, so leniency would only hide corruption. */
static const char B64_ALPHABET[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static int b64_val(char c)
{
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

/* -> decoded byte count, or -1 on any malformed input. The caller maps that to
 * SCPI_ERROR_INVALID_STRING_DATA (-151); never return a short read as if it
 * were data, because a silently short register dump decodes as a plausible
 * map of a codec we did not actually read. */
static int b64_decode(const char *in, size_t len, unsigned char *out, size_t cap)
{
    if (len == 0 || (len % 4) != 0) return -1;      /* unpadded is rejected */

    /* Padding belongs to the FINAL group only: one or two '=' at the very
     * end. The previous version counted pad once and then reused it for every
     * group, zeroing it after the first pass. Two consequences, both measured
     * by running this file's own decoder under gcc against Python's stdlib
     * base64 as the oracle: a non-final group emitted 3-pad bytes instead of
     * 3, and the final group's '=' guard then saw pad==0 and rejected
     * well-formed input. Net effect -- any payload with MORE THAN ONE 4-CHAR
     * GROUP that ended in '=' returned -1, while single-group payloads
     * ("Aw==") and every UNPADDED payload decoded fine. On hardware that read
     * as a dead transport: -1 makes the caller push an error and answer
     * nothing, so 1-, 4- and 5-address register reads timed out while
     * 2-, 3-, 6- and 12-address reads worked. Length correlated with the
     * failure only because 4- and 8-character payloads are what 1 and 4..5
     * addresses happen to encode to, which is what sent two rounds of
     * investigation down the framing and buffer-size paths. */
    size_t pad = 0;
    if (in[len - 1] == '=') { pad++; if (in[len - 2] == '=') pad++; }

    size_t n = 0;
    for (size_t i = 0; i < len; i += 4) {
        const int last = (i + 4 == len);
        unsigned acc = 0;
        for (unsigned k = 0; k < 4; k++) {
            char c = in[i + k];
            if (c == '=') {
                /* Legal only in the last group, and only in its trailing
                 * positions: '=' at k < 4-pad would be padding in front of
                 * data, which is not base64. */
                if (!last || k < 4u - pad) return -1;
                acc <<= 6;
                continue;
            }
            int v = b64_val(c);
            if (v < 0) return -1;
            acc = (acc << 6) | (unsigned)v;
        }
        /* A non-final group always yields its full 3 bytes; only the last
         * group is shortened by the padding it declared. */
        const int emit = last ? (int)(3u - pad) : 3;
        for (int k = 0; k < emit; k++) {
            if (n >= cap) return -1;                /* refuse, never truncate */
            out[n++] = (unsigned char)(acc >> (16 - 8 * k));
        }
    }
    return (int)n;
}

/* -> byte count written into out (not NUL-terminated), or 0 if cap too small. */
static size_t b64_encode(const unsigned char *in, size_t n, char *out, size_t cap)
{
    size_t need = ((n + 2u) / 3u) * 4u;
    if (need > cap) return 0;
    size_t o = 0;
    for (size_t i = 0; i < n; i += 3) {
        unsigned a = in[i];
        unsigned b = (i + 1 < n) ? in[i + 1] : 0;
        unsigned c = (i + 2 < n) ? in[i + 2] : 0;
        unsigned acc = (a << 16) | (b << 8) | c;
        out[o++] = B64_ALPHABET[(acc >> 18) & 0x3F];
        out[o++] = B64_ALPHABET[(acc >> 12) & 0x3F];
        out[o++] = (i + 1 < n) ? B64_ALPHABET[(acc >> 6) & 0x3F] : '=';
        out[o++] = (i + 2 < n) ? B64_ALPHABET[acc & 0x3F]         : '=';
    }
    return o;
}

/* ---- REGister:READ? / REGister:WRITE ---------------------------------- */

/* Both are forwarded to the tile-0 audio-hw task, which is the sole writer of
 * the codec I2C bus (see clearsounddac_board.xc). A second I2C client on this
 * core would race the volume/mute path and corrupt codec state -- the failure
 * would be audible and intermittent, the worst kind. Because it is a request
 * to another core, these two commands MAY BLOCK on that core's queue. That is
 * acceptable here and nowhere else: this core is not the audio path. */
/* REGister:READ? "<b64 addresses>"   ->  "<b64 values>"
 * REGister:WRITE "<b64 addr,val,...>"  (no reply; errors via the queue)
 *
 * Payload is base64 for the reasons documented at b64_decode. One parameter,
 * quoted, opaque: the syntax no longer depends on the value's digit count.
 *
 * The payload is a LIST, not a single byte, because continuous ranges are what
 * makes a codec dump possible in one round trip -- one blocked exchange with
 * tile 0 per command instead of one per register. Read: N address bytes in,
 * N value bytes out, positionally aligned. Write: 2N bytes of (addr,value)
 * pairs. N is capped because every register access is a request to the tile-0
 * I2C owner and this command therefore blocks: an unbounded list could stall
 * the diagnostic core long enough to starve the USB MIDI endpoint.
 *
 * Reads are all-or-nothing: any failed register aborts with no reply rather
 * than emitting a short buffer, because a truncated dump whose length was not
 * checked would decode as a plausible-looking register map. */
#define CSD_REG_MAX 64u

static SCPI_FPTR_CMD scpi_result_t reg_read_q(scpi_t *context)
{
    char txt[4 * CSD_REG_MAX + 8];
    unsigned char addrs[CSD_REG_MAX], vals[CSD_REG_MAX];
    char out[4 * CSD_REG_MAX + 8];

    if (!param_text(context, txt, sizeof txt, TRUE)) return SCPI_RES_ERR;
    int n = b64_decode(txt, strlen(txt), addrs, sizeof addrs);
    if (n <= 0) {
        SCPI_ErrorPush(context, SCPI_ERROR_INVALID_STRING_DATA);
        return SCPI_RES_ERR;
    }

    for (int i = 0; i < n; i++) {
        unsigned v = 0;
        if (!csd_diag_reg_read(addrs[i], &v)) { push_hw_error(context); return SCPI_RES_ERR; }
        vals[i] = (unsigned char)v;             /* ES9219Q regs are 8-bit */
    }

    size_t len = b64_encode(vals, (size_t)n, out, sizeof out);
    if (len == 0) { push_hw_error(context); return SCPI_RES_ERR; }
    SCPI_ResultCharacters(context, out, len);
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t reg_write(scpi_t *context)
{
    char txt[4 * (2 * CSD_REG_MAX) + 8];
    unsigned char buf[2 * CSD_REG_MAX];

    if (!param_text(context, txt, sizeof txt, TRUE)) return SCPI_RES_ERR;
    int n = b64_decode(txt, strlen(txt), buf, sizeof buf);
    if (n <= 0 || (n % 2) != 0) {               /* pairs, or nothing */
        SCPI_ErrorPush(context, SCPI_ERROR_INVALID_STRING_DATA);
        return SCPI_RES_ERR;
    }

    /* Writes are applied IN ORDER; a mid-list I2C failure aborts with a
     * hardware error and the registers already written stay written -- the
     * codec has no dry-run mode, so claiming atomicity here would be a lie.
     * The host can REGister:READ? the range back to see what landed. */
    for (int i = 0; i < n; i += 2) {
        if (!csd_diag_reg_write(buf[i], buf[i + 1])) { push_hw_error(context); return SCPI_RES_ERR; }
        csd_diag_note_reg_write();
    }
    return SCPI_RES_OK;
}

/* ---- DIAG:DSP:PRESet -------------------------------------------------- */
static SCPI_FPTR_CMD scpi_result_t q_dsp(scpi_t *context)
{
    char buf[16];
    unsigned n = fmt_u32((uint32_t) csd_dsp_get_preset(), buf, sizeof buf - 1u);
    if (n == 0) {                                 /* preset is 0..2: cannot
                                                   * overflow 15 chars */
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
        return SCPI_RES_ERR;
    }
    SCPI_ResultCharacters(context, buf, n);
    return SCPI_RES_OK;
}

/* DIAG:DSP:PRESet <n> -- the set half. The query half is q_dsp(), registered
 * as its own entry: libscpi matches a pattern against the command HEADER only
 * (findCommandHeader calls matchCommand(cmd->pattern, header, len, ...) with
 * the program data already split off), and a bare pattern does not match the
 * '?' form, so the two halves cannot share one entry.
 *
 * Why the pattern carries NO parameter placeholder: once the command side is
 * exhausted, any pattern text still unmatched fails the match unless it sits
 * in optional [...] brackets (utils.c: "if (pattern_len != 0) result =
 * FALSE"). So "<string>" is compared as if it were a keyword and can never
 * match -- libscpi has no placeholder syntax; the only in-pattern parameter
 * form is "#" for numeric keywords. Parameters are pulled here with
 * SCPI_Param*() (via param_text).
 *
 * This was the bug that made preset and register commands look dead: all three
 * parameterised commands answered -113 "Undefined header" with their callbacks
 * never running, in every spelling tried ("CMD 0", "CMD,0", "CMD \"0\"), while
 * the parameterless ones worked. Measured with
 * workspace/csd-diag/native_scpi_harness.c (real libscpi + this file, host
 * build) -- not with the Python --sim backend, which is a second
 * implementation and agrees only with itself. */
static SCPI_FPTR_CMD scpi_result_t set_dsp(scpi_t *context)
{
    char txt[16];
    uint32_t v;

    if (!param_text(context, txt, sizeof txt, TRUE)) return SCPI_RES_ERR;
    if (!parse_num(txt, strlen(txt), &v)) {
        SCPI_ErrorPush(context, SCPI_ERROR_INVALID_STRING_DATA);
        return SCPI_RES_ERR;
    }
    /* 0 OFF (byte-exact), 1 delay-only, 2 bass shelf -- see csd_dsp.h. The
     * upper bound matters: csd_dsp_set_preset() treats any value >= 2 as
     * "FIR preset", so a typo'd 7 would silently select the shelf. */
    if (v > 2u) {
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
    csd_dsp_set_preset((unsigned)v);
    return SCPI_RES_OK;
}

/* ---- identity access from this core ------------------------------------
 * Every handler below runs on the tile-1 diagnostic core; the identity lives
 * on tile[0]. Reading the csd_identity accessors directly from here returned
 * tile 1's own never-written copy of those .dp.bss statics -- so every query
 * answered "nothing read", the serial printed IPENDING and the staged
 * progress counter sat at 0 no matter what the boot read actually did. A
 * per-tile static is not a shared variable, and XC does not warn.
 *
 * So one fetch from the owner, over the chanend register access already uses
 * (see csd_diag_regs.xc). No handler touches the accessors.
 *
 * Fetching per command rather than caching is deliberate: caching would
 * reintroduce exactly the staleness this removes, and these are bench queries
 * issued a few at a time, not a hot path. */
static int ident_fetch(csd_id_snapshot_t *snap)
{
    return csd_diag_identity_snapshot(snap);
}

/* ---- *IDN? -------------------------------------------------------------
 * Our own handler rather than libscpi's SCPI_CoreIdnQ, for one reason: that
 * one prints the four strings captured at SCPI_Init(), and the serial is no
 * longer a constant. Capturing at init would freeze whatever the boot read
 * had published by then -- possibly the pending marker, permanently.
 *
 * It also cannot call csd_identity_serial_string() directly, for the tile
 * reason above: the serial has to come from the snapshot. Field order is the
 * IEEE 488.2 one: vendor, model, serial, firmware. */
static SCPI_FPTR_CMD scpi_result_t q_idn(scpi_t *context)
{
    csd_id_snapshot_t snap;
    SCPI_ResultMnemonic(context, CSD_IDN_VENDOR);
    SCPI_ResultMnemonic(context, CSD_IDN_MODEL);
    if (ident_fetch(&snap)) {
        SCPI_ResultMnemonic(context, snap.str);
    } else {
        /* Distinguish "could not ask the owner" from "the owner has nothing
         * yet": the first is a transport fault, the second is normal at boot. */
        SCPI_ResultMnemonic(context, "UNAVAILABLE");
    }
    SCPI_ResultMnemonic(context, CSD_IDN_FIRMWARE);
    return SCPI_RES_OK;
}

/* ---- DIAG:IDENTity -----------------------------------------------------
 * Read-only: the bench must be able to ask "which board is this, and how much
 * can I trust that answer" without a firmware round trip, and without ever
 * risking an OTP write.
 *
 * Five queries rather than one, because the answers carry different trust
 * levels and collapsing them would let a DERIVED fingerprint be recorded as if
 * it were a provisioned serial:
 *   SERial?  the label the device is wearing (same string as USB iSerial)
 *   SOURce?  OTP (true unit identity) | DERIVED (board fingerprint, NOT
 *            unique) | NONE (read did not complete)
 *   HASH?    the 64-bit FNV-1a over the material, for the bench log
 *   RAW?     the material words, base64, so the host can recompute the hash
 *            itself instead of trusting our arithmetic
 *   STAGe?   how far the staged read got -- the only way to tell a blank OTP
 *            from a stalled read, since both leave the material all zero */
static SCPI_FPTR_CMD scpi_result_t q_ident_serial(scpi_t *context)
{
    csd_id_snapshot_t snap;
    if (!ident_fetch(&snap)) { push_hw_error(context); return SCPI_RES_ERR; }
    size_t n = strlen(snap.str);
    if (n == 0) { SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR); return SCPI_RES_ERR; }
    SCPI_ResultCharacters(context, snap.str, n);
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t q_ident_source(scpi_t *context)
{
    csd_id_snapshot_t snap;
    const char *s;
    if (!ident_fetch(&snap)) { push_hw_error(context); return SCPI_RES_ERR; }
    switch ((csd_id_source_t)snap.source) {
        case CSD_ID_OTP:     s = "OTP";     break;   /* provisioned: trust it  */
        case CSD_ID_DERIVED: s = "DERIVED"; break;   /* NOT unique: bench only */
        default:             s = "NONE";    break;   /* read did not complete  */
    }
    SCPI_ResultCharacters(context, s, strlen(s));
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t q_ident_hash(scpi_t *context)
{
    csd_id_snapshot_t snap;
    if (!ident_fetch(&snap)) { push_hw_error(context); return SCPI_RES_ERR; }

    /* Fixed-width 16 digits, deliberately NOT built from fmt_u32_hex: that
     * formatter emits MINIMAL digits, so a zero high word would splice as
     * "0x0"+"12345678" = 0x012345678 -- the same number 16x too large, and it
     * would look right. Same class of trap as the numid=5 read-back that lied
     * to the volume gate: emit on a fixed grid or not at all. */
    static const char d[] = "0123456789ABCDEF";
    char out[2 + 16 + 1];
    out[0] = '0'; out[1] = 'x';
    for (unsigned i = 0; i < 16; i++) {
        out[2 + 15 - i] = d[(snap.hash >> (4u * i)) & 0xFu];
    }
    out[18] = 0;
    SCPI_ResultCharacters(context, out, 18);
    return SCPI_RES_OK;
}

/* Material words, big-endian, base64 -- same payload convention as
 * REGister:READ? and the same reason: one opaque token, no dependence on how
 * many digits a value happens to have, and exactly one quoted string so the
 * client can always take the last. */
static SCPI_FPTR_CMD scpi_result_t q_ident_raw(scpi_t *context)
{
    csd_id_snapshot_t snap;
    if (!ident_fetch(&snap)) { push_hw_error(context); return SCPI_RES_ERR; }
    if (!snap.ready) {
        /* Distinct from "all zeros": a blank board answers RAW? with 28 zero
         * bytes. -200 here means the owner has not finished reading, which on
         * a bench board should instead be visible as a stage count. */
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
        return SCPI_RES_ERR;
    }
    unsigned char be[CSD_ID_MATERIAL_WORDS * 4];
    for (unsigned i = 0; i < CSD_ID_MATERIAL_WORDS; i++) {
        be[i * 4 + 0] = (unsigned char)(snap.words[i] >> 24);
        be[i * 4 + 1] = (unsigned char)(snap.words[i] >> 16);
        be[i * 4 + 2] = (unsigned char)(snap.words[i] >> 8);
        be[i * 4 + 3] = (unsigned char)(snap.words[i]);
    }
    char out[((CSD_ID_MATERIAL_WORDS * 4 + 2) / 3) * 4 + 4];
    size_t len = b64_encode(be, sizeof be, out, sizeof out);
    if (len == 0) { SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR); return SCPI_RES_ERR; }
    SCPI_ResultCharacters(context, out, len);
    return SCPI_RES_OK;
}

/* How far the staged read got: 0 not started, 1 entered, 2..5 one per id
 * read, 6 complete. This exists because on a virgin board every material word
 * is legitimately 0, so RAW? alone cannot distinguish "OTP is blank" from "the
 * read stalled at the first access" -- and those two demand opposite
 * responses. */
static SCPI_FPTR_CMD scpi_result_t q_ident_stage(scpi_t *context)
{
    csd_id_snapshot_t snap;
    if (!ident_fetch(&snap)) { push_hw_error(context); return SCPI_RES_ERR; }
    char buf[8];
    unsigned n = fmt_u32(snap.stage, buf, sizeof buf - 1u);
    if (n == 0) { SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR); return SCPI_RES_ERR; }
    SCPI_ResultCharacters(context, buf, n);
    return SCPI_RES_OK;
}

/* ---- command table ---------------------------------------------------- */
/* ---- function generator -------------------------------------------------
 *
 * Units, all exact integers, because parse_num() here is unsigned-integer
 * only (decimal, or 0x/0b/h suffixed) and there is no float in the image and
 * no reason to introduce soft-float for an argument parser:
 *
 *   frequency  milliHz    20 kHz = 20000000, 1 Hz = 1000; fits u32 with room
 *   period     ms         24 h = 86400000; fits u32
 *   sample rate Hz        one of the advertised altsettings
 *   amplitude  Q31 peak   0x40000000 = -6 dBFS, the same convention every
 *                         sample in this codebase uses
 *
 * Amplitude is deliberately NOT in dB. Accepting dB would need 10^(x/20) in
 * fixed point -- a new table, a new quantisation, and a new way to be subtly
 * wrong -- for a knob whose meaningful precision is one bit of a 31-bit
 * number. The rig works in dB already (the acoustic gate measures dBFS from
 * captured samples), so dB is the host's unit and Q31 is the device's.
 *
 * Frequencies are echoed back through GENerator:STATe? as the ADOPTED Q8
 * value, which is the frequency the oscillator actually produces: Q8 is
 * 1/256 Hz, so a request for 1000.001 Hz becomes 1000.0000 and the query says
 * so, rather than leaving the host to discover 0.001 Hz by measurement.
 */

static const unsigned csd_gen_hz_max = 20000000u;   /* 20 kHz in milliHz */

static int gen_mhz(scpi_t *context, uint32_t *out_q8, const char **why)
{
    char txt[16];
    uint32_t mhz;
    if (!param_text(context, txt, sizeof txt, TRUE)) {
        *why = "missing frequency";
        return 0;
    }
    if (!parse_num(txt, strlen(txt), &mhz)) {
        *why = "frequency not a number";
        return 0;
    }
    /* Range asserted in milliHz, before the Q8 conversion, so the limit the
     * host is told about is the limit the host used. 20 kHz is the spec;
     * Nyquist is checked separately by the generator, which is the authority
     * on what fits at the sample rate actually in force. */
    if (mhz < 1000u || mhz > csd_gen_hz_max) {
        *why = "frequency outside 1 Hz .. 20 kHz";
        return 0;
    }
    /* milliHz -> Q8 Hz: mhz * 256 / 1000. Rounded, not truncated: truncating
     * would bias every frequency the host asked for slightly low, which reads
     * as a measurement problem rather than a rounding choice. */
    *out_q8 = (uint32_t)(((uint64_t)mhz * (uint64_t)CSD_GEN_HZ_SCALE + 500ull)
                       / 1000ull);
    return 1;
}

static int gen_u32(scpi_t *context, const char *name, uint32_t *out,
                   uint32_t lo, uint32_t hi, const char **why)
{
    char txt[16];
    if (!param_text(context, txt, sizeof txt, TRUE)) {
        *why = name;
        return 0;
    }
    if (!parse_num(txt, strlen(txt), out)) {
        *why = name;
        return 0;
    }
    if (*out < lo || *out > hi) {
        *why = name;
        return 0;
    }
    return 1;
}

/* Case-insensitive keyword compare.
 *
 * Not strcasecmp(): that is POSIX, and this file includes only <string.h> and
 * <stdint.h> deliberately (see the note above fmt_u32), so relying on it here
 * would either fail to link on xcore or silently work on the host and not on
 * the target -- the worst failure order available. Four characters, and the
 * keywords are known-length, so there is nothing clever to get wrong. */
static int eq_ci(const char *a, const char *kw)
{
    for (; *kw; a++, kw++) {
        char c = *a;
        if (c >= 'a' && c <= 'z') c = (char)(c - 'a' + 'A');
        if (c != *kw) return 0;             /* keywords stored uppercase */
    }
    return *a == 0;
}

/* Shared tail for every arm command: report the rejection reason through the
 * SCPI error queue rather than answering OK and staying silent. The generator
 * refuses rather than clamping, so a typo'd argument must be an error the host
 * can read -- not a waveform that looks plausible.
 *
 * Only error codes present in the MINIMAL list are usable: this build has
 * USE_FULL_ERROR_LIST=0 (scpi_user_config.h), and in libscpi's error.h the
 * full-list-only entries are marked XE -- SCPI_ERROR_DATA_OUT_OF_RANGE (-222)
 * is one of them, so naming it would not compile. -224 and -200 are X
 * entries. The fine-grained reason is not lost: it is readable from
 * GENerator:STATe:REJect?, which is the same information without needing the
 * library to carry a bigger string table. */
static scpi_result_t gen_report(scpi_t *context, int rc, int why)
{
    if (rc == 0) return SCPI_RES_OK;
    switch (why) {
    case CSD_GEN_REJ_FREQ_RANGE:
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        break;
    default:
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
        break;
    }
    return SCPI_RES_ERR;
}

/* GENerator:SINe <fs>,<freq_mHz>,<amp>[,<period_ms>]
 * GENerator:SQUare <fs>,<freq_mHz>,<amp>[,<period_ms>]
 *
 * period_ms omitted or 0 = hold indefinitely (the tone/square case). Giving
 * one makes it a finite burst, which is what the acoustic rig wants: a burst
 * with known endpoints so the capture knows where to look. */
static scpi_result_t gen_tone(scpi_t *context, unsigned mode)
{
    const char *why = "argument";
    uint32_t fs, fq8, amp, per = 0u;
    int rwhy = 0;

    if (!gen_u32(context, "sample rate", &fs, 1u, 1000000u, &why)) goto bad;
    if (!gen_mhz(context, &fq8, &why)) goto bad;
    if (!gen_u32(context, "amplitude", &amp,
                 CSD_GEN_AMP_MIN, CSD_GEN_AMP_MAX, &why)) goto bad;
    /* Optional argument: absent means hold forever.
     *
     * Read ONCE, then parsed from the copy. An earlier version peeked with
     * SCPI_ParamCharacters(mandatory=FALSE) and, if something was there,
     * called gen_u32() to read it again -- which cannot work: SCPI_Parameter
     * advances the lexer on every call, so the second read was already at
     * end-of-input, pushed -109 Missing parameter, and the command failed
     * with calls=0. The harness caught it (build_native_scpi.sh,
     * "GENerator:SINe 48000,1000000,1073741824,500"), not the board.
     *
     * "Absent" is told from "malformed" by the error-queue delta, because
     * SCPI_Parameter returns FALSE for BOTH "no more input" (pos past the
     * buffer) and a bad separator (it pushes -108 and returns FALSE). Skipping
     * on FALSE alone would read a mistyped burst length as "no burst
     * requested" and arm a steady tone in silence. The absent case pushes
     * nothing; the malformed case pushes something; the delta is the test.
     * Both behaviours read out of libscpi parser.c, not inferred from the
     * signature, which cannot distinguish them. */
    {
        const char *pv;
        size_t pl = 0;
        int32_t e_before = SCPI_ErrorCount(context);
        scpi_bool_t got = SCPI_ParamCharacters(context, &pv, &pl, FALSE);
        if (SCPI_ErrorCount(context) != e_before) goto bad;   /* malformed */
        if (got && pl > 0) {
            char txt[16];
            uint32_t v;
            if (pl >= sizeof txt) { why = "period too long"; goto bad; }
            memcpy(txt, pv, pl);
            txt[pl] = 0;
            if (!parse_num(txt, pl, &v)) { why = "period"; goto bad; }
            if (v > CSD_GEN_MAX_PERIOD_MS) { why = "period"; goto bad; }
            per = v;
        }
    }

    return gen_report(context,
        csd_gen_request_arm(mode, fs, fq8, 0u, CSD_GEN_DOMAIN_LIN,
                            amp, per, &rwhy), rwhy);
bad:
    SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
    (void)why;
    return SCPI_RES_ERR;
}

static SCPI_FPTR_CMD scpi_result_t gen_sine(scpi_t *context)
{
    return gen_tone(context, CSD_GEN_MODE_SINE);
}

static SCPI_FPTR_CMD scpi_result_t gen_square(scpi_t *context)
{
    return gen_tone(context, CSD_GEN_MODE_SQUARE);
}

/* GENerator:SWEep <fs>,<f_start_mHz>,<f_stop_mHz>,<LIN|LOG>,<amp>,<period_ms>
 *
 * One continuous function of the frame index, f(n) = f_start *
 * (f_stop/f_start)^(n/N) -- not a sequence of packets, which was the original
 * design and was rejected: a stepped sweep has a spectrum of its own and the
 * DUT gets measured against that instead of against a chirp.
 *
 * f_start and f_stop are unconstrained relative to each other, which is the
 * whole point of the formula: f_start < f_stop rises, f_start > f_stop falls,
 * f_start == f_stop holds. Three behaviours, one code path, no direction flag
 * for a caller to get wrong. */
static SCPI_FPTR_CMD scpi_result_t gen_sweep(scpi_t *context)
{
    const char *why = "argument";
    uint32_t fs, fa, fb, amp, per;
    uint32_t domain;
    int rwhy = 0;
    char dom[8];

    if (!gen_u32(context, "sample rate", &fs, 1u, 1000000u, &why)) goto bad;
    if (!gen_mhz(context, &fa, &why)) goto bad;
    if (!gen_mhz(context, &fb, &why)) goto bad;
    if (!param_text(context, dom, sizeof dom, TRUE)) { why = "domain"; goto bad; }
    if (eq_ci(dom, "LIN"))      domain = CSD_GEN_DOMAIN_LIN;
    else if (eq_ci(dom, "LOG")) domain = CSD_GEN_DOMAIN_LOG;
    else { why = "domain must be LIN or LOG"; goto bad; }
    if (!gen_u32(context, "amplitude", &amp,
                 CSD_GEN_AMP_MIN, CSD_GEN_AMP_MAX, &why)) goto bad;
    /* A sweep without a period has no meaning: the exponent n/N needs N.
     * Refused rather than defaulted, because a defaulted length would be a
     * 0.5 s sweep where the operator meant 30 s. */
    if (!gen_u32(context, "period", &per, 1u, CSD_GEN_MAX_PERIOD_MS, &why))
        goto bad;

    return gen_report(context,
        csd_gen_request_arm(CSD_GEN_MODE_SWEEP, fs, fa, fb, domain,
                            amp, per, &rwhy), rwhy);
bad:
    SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
    (void)why;
    return SCPI_RES_ERR;
}

/* GENerator:TONEs <fs>,<amp>,<period_ms>,<count>,<f1>,<f2>[,<f3>,<f4>]
 *
 * count is 2..4 and exactly that many frequencies must follow. Not clamped:
 * a 7 would otherwise become an unremarkable 4-tone result and the operator
 * would measure four tones believing they had asked for seven. */
static SCPI_FPTR_CMD scpi_result_t gen_tones(scpi_t *context)
{
    const char *why = "argument";
    uint32_t fs, amp, per, count;
    uint32_t fq[CSD_GEN_MAX_TONES_PUB];
    int rwhy = 0;

    if (!gen_u32(context, "sample rate", &fs, 1u, 1000000u, &why)) goto bad;
    if (!gen_u32(context, "amplitude", &amp,
                 CSD_GEN_AMP_MIN, CSD_GEN_AMP_MAX, &why)) goto bad;
    if (!gen_u32(context, "period", &per, 0u, CSD_GEN_MAX_PERIOD_MS, &why))
        goto bad;
    if (!gen_u32(context, "tone count", &count, 2u, CSD_GEN_MAX_TONES_PUB, &why))
        goto bad;

    /* Each tone carries 1/count of the full scale inside the generator, so
     * `amp` is the peak of the SUM: the composite stays at the requested
     * level as tones are added, which is what makes a 2-tone and a 4-tone
     * measurement comparable without recomputing levels by hand. */
    for (uint32_t i = 0; i < count; i++)
        if (!gen_mhz(context, &fq[i], &why)) goto bad;

    return gen_report(context,
        csd_gen_request_tones(fs, count, fq, amp, per, &rwhy), rwhy);
bad:
    SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
    (void)why;
    return SCPI_RES_ERR;
}

/* GENerator:NOISe <fs>,<amp>,<period_ms> */
static SCPI_FPTR_CMD scpi_result_t gen_noise(scpi_t *context)
{
    const char *why = "argument";
    uint32_t fs, amp, per;
    int rwhy = 0;

    if (!gen_u32(context, "sample rate", &fs, 1u, 1000000u, &why)) goto bad;
    if (!gen_u32(context, "amplitude", &amp,
                 CSD_GEN_AMP_MIN, CSD_GEN_AMP_MAX, &why)) goto bad;
    if (!gen_u32(context, "period", &per, 0u, CSD_GEN_MAX_PERIOD_MS, &why))
        goto bad;

    return gen_report(context,
        csd_gen_request_arm(CSD_GEN_MODE_NOISE, fs, CSD_GEN_HZ_SCALE /* 1 Hz, the
                                                           noise mode
                                                           ignores it */,
                            0u,
                            CSD_GEN_DOMAIN_LIN, amp, per, &rwhy), rwhy);
bad:
    SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
    (void)why;
    return SCPI_RES_ERR;
}

/* GENerator:DC <fs>,<amp>,<period_ms>  (TODO item 2, directive 2026-11-01)
 *
 * Constant offset: every frame both channels carry exactly <amp> (Q31,
 * same scale as the other modes; 0x7FFFFFFF = +full scale). The DC case in
 * csd_gen_sample ignores frequency, so f_a is the 1 Hz placeholder the
 * shared validators demand -- same shape as noise. period_ms = 0 holds
 * indefinitely; a non-zero value is a timed burst, and like every other
 * mode csd_gen_off / a new command ends it. Negative DC is out of scope:
 * amp is unsigned by contract, and the DUT-relevant stimulus sweep stays
 * positive; if a bipolar offset is ever needed it is its own command, not
 * a sign bit smuggled through the shared amplitude gate. */
static SCPI_FPTR_CMD scpi_result_t gen_dc(scpi_t *context)
{
    const char *why = "argument";
    uint32_t fs, amp, per;
    int rwhy = 0;

    if (!gen_u32(context, "sample rate", &fs, 1u, 1000000u, &why)) goto bad;
    if (!gen_u32(context, "amplitude", &amp,
                 CSD_GEN_AMP_MIN, CSD_GEN_AMP_MAX, &why)) goto bad;
    if (!gen_u32(context, "period", &per, 0u, CSD_GEN_MAX_PERIOD_MS, &why))
        goto bad;

    return gen_report(context,
        csd_gen_request_arm(CSD_GEN_MODE_DC, fs, CSD_GEN_HZ_SCALE /* 1 Hz,
                                                          ignored by DC */,
                            0u,
                            CSD_GEN_DOMAIN_LIN, amp, per, &rwhy), rwhy);
bad:
    SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
    (void)why;
    return SCPI_RES_ERR;
}

/* GENerator:OFF */
static SCPI_FPTR_CMD scpi_result_t gen_off(scpi_t *context)
{
    (void)context;
    csd_gen_request_off();
    return SCPI_RES_OK;
}

/* GENerator:STATe? -> mode,fs,f_a_hz_q8,f_b_hz_q8,amp,domain,tones,cmd,adopted
 *
 * Nine integers, host-parseable, no formatting cleverness. The last two are
 * the point: cmd advances when a command is accepted, adopted advances when
 * the AUDIO thread picks it up at a frame boundary. cmd != adopted means the
 * stream is not running, which is the difference between "the generator is
 * broken" and "there is no stream" -- a distinction that otherwise costs an
 * afternoon, because both look like silence. */
static SCPI_FPTR_CMD scpi_result_t gen_state_q(scpi_t *context)
{
    /* fmt_u32 + a hand-rolled joiner, not snprintf: this file includes no
     * <stdio.h> on purpose (see the note above fmt_u32) -- printf drags in
     * kilobytes of flash and, on audio threads, the xscope backpressure that
     * cost the silent-DAC week. Nine fields, so the buffer is sized from the
     * worst case: nine 10-digit values plus eight commas plus NUL. */
    char buf[9 * 10 + 9];
    unsigned v[9];
    unsigned len = 0;

    v[0] = csd_gen_state_mode();
    v[1] = csd_gen_state_fs();
    v[2] = csd_gen_state_f_a_q8();
    v[3] = csd_gen_state_f_b_q8();
    v[4] = csd_gen_state_amp();
    v[5] = csd_gen_state_domain();
    v[6] = csd_gen_state_tones();
    v[7] = csd_gen_state_cmd();
    v[8] = csd_gen_state_adopted();

    for (unsigned i = 0; i < 9u; i++) {
        if (i) buf[len++] = ',';
        unsigned n = fmt_u32(v[i], buf + len, (unsigned)sizeof buf - len);
        if (n == 0) {                       /* would not fit */
            SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
            return SCPI_RES_ERR;
        }
        len += n;
    }
    SCPI_ResultCharacters(context, buf, len);
    return SCPI_RES_OK;
}

/* ---- DEBUG-only instruments (TODO item 5, directive 2026-11-01) ----------
 * Bench counters added during bring-up. Compiled out entirely when
 * SPECIALTYCIRCUITS_RELEASE is defined (the release switch, TODO item 6):
 * callbacks AND table rows vanish, so a stray DEBUG:/MIXer:counter command
 * in a release build answers -113 "Undefined header", and the code costs no
 * flash. The production surface (*IDN?, error queue, DIAG:DSP:PRESet,
 * DIAG:IDENTity:*, REGister:*, GENerator: waveform setters + STATe?,
 * MIXer:GAIN:*) stays in every build. */
#ifndef SPECIALTYCIRCUITS_RELEASE
/* GENerator:COUNts? -> emit,sup_dsd,sup_dop
 *
 * What the frame hook decided per frame while the generator was armed:
 * wave emitted / suppressed by DSD-native mode / suppressed by the DoP
 * latch. These are the rig's proof for the DSD/DoP suppression requirement:
 * the mic capture path cannot judge it at DoP rates (the EARS codec is
 * 48 kHz-fixed; arecord resamples silently and the spectrum computed
 * against the requested rate is fiction -- it produced a false FAIL once),
 * while the counters are taken on the audio thread at the decision point.
 * Deltas across a stream segment are the measurement; values never reset. */
static SCPI_FPTR_CMD scpi_result_t gen_counts_q(scpi_t *context)
{
    char buf[5 * 10 + 5];
    unsigned v[5];
    unsigned len = 0;

    v[0] = csd_gen_cnt_emit();
    v[1] = csd_gen_cnt_sup_dsd();
    v[2] = csd_gen_cnt_sup_dop();
    v[3] = csd_dsp_diag_dsd_flag();   /* raw stream mode (0/1/2) now        */
    v[4] = csd_dsp_diag_dop_latched();/* our in-band latch now             */
    for (unsigned i = 0; i < 5u; i++) {
        if (i) buf[len++] = ',';
        unsigned n = fmt_u32(v[i], buf + len, (unsigned)sizeof buf - len);
        if (n == 0) {
            SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
            return SCPI_RES_ERR;
        }
        len += n;
    }
    SCPI_ResultCharacters(context, buf, len);
    return SCPI_RES_OK;
}

/* GENerator:VOLume? -> cmds,last_index,last_multiplier
 *
 * Did the host's SET_CUR for 'Mix 2 Playback Volume' reach the mixer at all?
 * amixer returns rc=0 and the control then reads back 0 counts (-48 dB = our
 * MIN_VOLUME), never the value written, on builds with and without the
 * generator -- so the question is live and three instruments have already
 * failed to answer it:
 *   - amixer cget: snd-usb-mixer caches what it wrote, so it can report the
 *     kernel's belief rather than the device's;
 *   - raw pyusb control transfers: EIO against our build AND against the
 *     factory EEPROM image, i.e. the transfers never landed -- an instrument
 *     that "fails" identically on a control group has measured nothing;
 *   - the xrun console: zero lines even at boot, so no printf reaches the
 *     host in this configuration, and the AudioHwRemote_Volume_Update trace
 *     print sits in the #else branch of updateVol, compiled out entirely
 *     because XUA_OUT_VOLUME_IN_MIXER is 1.
 * This counts the commands at their destination, the mixer's SET_MIX_OUT_VOL
 * handler, and reports them over SCPI -- the one transport proven on this
 * board (the whole 51-check rig rides it). cmds == 0 across a host write
 * means the command never arrived; cmds advancing with a sane multiplier
 * means it did, and the fault is downstream of the mixer.
 *
 * Separate command rather than extra fields on GENerator:COUNts?: that
 * reply's 5-field shape is asserted by the rig's parser, and widening a
 * proven contract to carry a diagnostic risks the suite that proves the
 * generator.
 */
static SCPI_FPTR_CMD scpi_result_t gen_volume_q(scpi_t *context)
{
    char buf[3 * 10 + 3];
    unsigned v[3];
    unsigned len = 0;

    v[0] = csd_mb_get_vol_cmds();
    v[1] = csd_mb_get_vol_index();
    v[2] = csd_mb_get_vol_val();
    for (unsigned i = 0; i < 3u; i++) {
        if (i) buf[len++] = ',';
        unsigned n = fmt_u32(v[i], buf + len, (unsigned)sizeof buf - len);
        if (n == 0) {
            SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
            return SCPI_RES_ERR;
        }
        len += n;
    }
    SCPI_ResultCharacters(context, buf, len);
    return SCPI_RES_OK;
}

#endif /* !SPECIALTYCIRCUITS_RELEASE: gen_counts_q / gen_volume_q */

/* MIXer:GAIN:HOST <w_q15> | MIXer:GAIN:GENerator <w_q15>
 * MIXer:GAIN? -> w_host,w_gen
 *
 * Individual volume control for the two pre-mixer sources, INSIDE the
 * mixer graph (user requirement: not the codec register, not the single
 * UAC output multiplier that moves both together). Q15, 32768 = unity;
 * range 0..2^16-1 allows a 6 dB boost before the saturator in
 * csd_gen_mix_scale takes over (T9-verified, host gate).
 *
 * Each setter reads the packed word, swaps its half, and writes it back --
 * read-modify-write from the single writer thread (diagnostic), so the
 * mixer thread's single read per frame never sees a torn pair. Outside a
 * mixer build the mailbox word still exists but nothing reads it; the
 * commands still round-trip, which is what the native harness checks.
 */
static SCPI_FPTR_CMD scpi_result_t mixer_gain_set(scpi_t *context, unsigned which_gen)
{
    const char *why = "argument";
    uint32_t w;
    if (!gen_u32(context, "gain_q15", &w, 0u, 65535u, &why)) {
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
    unsigned packed = csd_mb_get_gains();
    packed = which_gen ? ((w << 16) | (packed & 0xFFFFu))
                       : ((packed & 0xFFFF0000u) | w);
    csd_mb_put_gains(packed);
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t mixer_gain_host(scpi_t *context)
{ return mixer_gain_set(context, 0u); }

static SCPI_FPTR_CMD scpi_result_t mixer_gain_gen(scpi_t *context)
{ return mixer_gain_set(context, 1u); }

static SCPI_FPTR_CMD scpi_result_t mixer_gain_q(scpi_t *context)
{
    char buf[2 * 5 + 1];
    unsigned packed = csd_mb_get_gains();
    const unsigned v[2] = { packed & 0xFFFFu, packed >> 16 };
    unsigned len = 0;
    for (unsigned i = 0; i < 2u; i++) {
        if (i) buf[len++] = ',';
        unsigned n = fmt_u32(v[i], buf + len, (unsigned)sizeof buf - len);
        if (n == 0) {
            SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
            return SCPI_RES_ERR;
        }
        len += n;
    }
    SCPI_ResultCharacters(context, buf, len);
    return SCPI_RES_OK;
}

/* VOLume:TRIM:LEFT <counts> | VOLume:TRIM:RIGHT <counts>
 * VOLume:TRIM? -> countsL,countsR   (TODO items 3+4, directive 2026-11-01)
 *
 * <counts> = number of 0.125 dB steps, SIGNED, 0 = 0 dB down to -256 =
 * -32 dB (counts*32 = the 8.8 dB value the volume path consumes). The grid
 * is enforced by construction: a count is a count, so 0.1 dB is
 * inexpressible rather than rounded. 0 dB is the maximum, never positive.
 *
 * Accepted in every build (production calibration surface, like
 * MIXer:GAIN:*). The setter writes the packed mailbox word and raises the
 * persistence request flag; the tile[0] store task journals it within ~50 ms
 * (csd_trim_store.h -- this thread must not touch flash itself). The mixer
 * reads the word on every output-volume multiply (csd_mix_apply_trim), the
 * application point the directive names: host SET_CUR and button paths alike.
 *
 * VOLume:TRIM:STATus? reports the persistence layer's view (partition size,
 * load/save status, sequence, error code, counts) so a rig can tell
 * "persisted" from "silently never tried" -- the failure modes here are
 * silent by nature. */
static scpi_result_t vol_trim_set(scpi_t *context, unsigned which_right)
{
    const char *why = "argument";
    char txt[16];
    int32_t counts;

    if (!param_text(context, txt, sizeof txt, TRUE)) { why = "trim"; goto bad; }
    if (!parse_snum(txt, strlen(txt), &counts))      { why = "trim"; goto bad; }
    if (counts > 0 || counts < -256)                 { why = "trim"; goto bad; }

    unsigned packed = csd_mb_get_trim();
    unsigned v = (unsigned)(counts * 32);            /* int16 in 8.8 dB */
    packed = which_right ? ((v << 16) | (packed & 0xFFFFu))
                         : ((packed & 0xFFFF0000u) | v);
    csd_mb_put_trim(packed);
    /* Journal through the tile[0] server (csd_diag_regs.xc) -- this core
     * must not touch the SQI ports. Synchronous by design: the reply means
     * the value is in flash (or the status says why not), so a rig can read
     * VOLume:TRIM:STATus? immediately after this returns and believe it.
     * A rejected journal is NOT silently swallowed -- the trim still stands
     * for this power cycle, and the error queue reports that it did not
     * persist, so "set but not saved" can never look like "saved". */
    if (!csd_diag_trim_save()) {
        /* libscpi has no hardware-failure code; EXECUTION_ERROR is what this
         * file already uses for an operation that did not happen. The rig
         * reads SYST:ERR? to tell "saved" from "set but not journalled". */
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
    }
    return SCPI_RES_OK;
bad:
    SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
    (void)why;
    return SCPI_RES_ERR;
}

static SCPI_FPTR_CMD scpi_result_t vol_trim_left(scpi_t *context)
{
    return vol_trim_set(context, 0u);
}

static SCPI_FPTR_CMD scpi_result_t vol_trim_right(scpi_t *context)
{
    return vol_trim_set(context, 1u);
}

static SCPI_FPTR_CMD scpi_result_t vol_trim_q(scpi_t *context)
{
    unsigned packed = csd_mb_get_trim();
    int16_t tl = (int16_t)(packed & 0xFFFFu);
    int16_t tr = (int16_t)(packed >> 16);
    char buf[32];
    unsigned len = 0;
    /* signed formatter: fmt_u32 handles magnitude, the sign is one byte. */
    int32_t v[2] = { (int32_t)tl / 32, (int32_t)tr / 32 };   /* 0.125 dB counts */
    for (unsigned i = 0; i < 2u; i++) {
        if (len) buf[len++] = ',';
        int32_t x = v[i];
        unsigned n;
        if (x < 0) { buf[len++] = '-'; n = fmt_u32((uint32_t)(-(int64_t)x), buf + len,
                                                   (unsigned)sizeof buf - len); }
        else       { n = fmt_u32((uint32_t)x, buf + len, (unsigned)sizeof buf - len); }
        if (n == 0) {
            SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
            return SCPI_RES_ERR;
        }
        len += n;
    }
    SCPI_ResultCharacters(context, buf, len);
    return SCPI_RES_OK;
}

/* VOLume:TRIM:STATus? -> partition_bytes, load_status, save_status, seq,
 * last_error, boot_count, save_count        (TODO item 3 rig dependency)
 *
 * WHY THIS EXISTS RATHER THAN BEING AN OPTIONAL EXTRA
 * Every persistence failure in csd_trim_store.c is silent by design: a write
 * that fails simply means the trim does not persist, and the audio path
 * cannot tell. So without these seven numbers a rig cannot distinguish
 * "the record is in flash" from "the board has no data partition" from
 * "fl_connect() failed on a busy clock block" -- and the reboot-survival leg
 * would then be measuring the 0 dB / 0 dB reset default while believing it
 * measured persistence. That is exactly the vacuous-pass shape this project
 * has been bitten by twice. The rig asserts on these, not on the trim alone.
 *
 * Reads C globals written by the tile[0] store task -- the same shared-word
 * pattern csd_identity.xc uses for its stage counter, and legal for the same
 * reason: they are C statics in a C translation unit, not XC objects. */
static SCPI_FPTR_CMD scpi_result_t vol_trim_status_q(scpi_t *context)
{
    char buf[96];
    unsigned len = 0;
    for (unsigned i = 0; i < 7u; i++) {
        if (len) buf[len++] = ',';
        /* One scalar per round trip over the codec-server channel: the
         * cross-tile rule in this codebase is scalars only, never pointers
         * (csd_identity.h), so there is no array to hand across. */
        unsigned n = fmt_u32(csd_diag_trim_status(i), buf + len,
                             (unsigned)sizeof buf - len);
        if (n == 0) {
            SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
            return SCPI_RES_ERR;
        }
        len += n;
    }
    SCPI_ResultCharacters(context, buf, len);
    return SCPI_RES_OK;
}

/* VOLume:TRIM:LOAD -> re-read the journal into the mailbox.
 * A rig uses this to test "adopt what is in flash" without restarting the
 * firmware: journal a pair, set something else, load, and the pair must be
 * back. Complements the reload leg (rig/trim_persist_run.sh), which is the
 * only thing that proves survival across a boot. */
static SCPI_FPTR_CMD scpi_result_t vol_trim_load(scpi_t *context)
{
    if (!csd_diag_trim_load()) {
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
        return SCPI_RES_ERR;
    }
    return SCPI_RES_OK;
}

#ifndef SPECIALTYCIRCUITS_RELEASE   /* debug-only block, see above */
/* MIXer:BUSY? -- peak ticks mixer1 held audio() between the request token
 * and the sample exchange. Direct answer to "does the generator, which runs
 * INSIDE mixer1 on tile[1], steal budget from the hard-real-time audio()
 * task sharing that tile": 12500 ticks is a whole 48 kHz sample period, so
 * anything approaching it means a missed I2S deadline -- the same missed
 * deadline that keeps the decouple pre-filling with the token word. Read it
 * either side of a streamed-zero file; compare gen-on vs gen-off builds. */
/* MIXer:HWCfg? -> count,lastFreq,lastMClk: proof csd_AudioHwConfig ran.
 * Every "did the bring-up cause it?" conclusion needs this, because ALSA's
 * plughw silently resamples and a rate-changing leg may never issue a USB
 * SET_CUR at all. */
static SCPI_FPTR_CMD scpi_result_t mixer_hwcfg_q(scpi_t *context)
{
    char buf[3 * 10 + 3];
    unsigned len = 0;
    unsigned cnt = 0, fr = 0, mc = 0;
    csd_mb_get_hwcfg(&cnt, &fr, &mc);
    /* tail peak rides as a 4th value: same instrument, one round trip */
    const unsigned v[3] = { cnt, fr, mc };
    for (unsigned i = 0; i < 3u; i++) {
        if (len) buf[len++] = ',';
        unsigned n = fmt_u32(v[i], buf + len, (unsigned)sizeof buf - len);
        if (n == 0) {
            SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
            return SCPI_RES_ERR;
        }
        len += n;
    }
    SCPI_ResultCharacters(context, buf, len);
    return SCPI_RES_OK;
}

/* MIXer:TAIL? -- peak ticks mixer1 spent AFTER unblocking audio() (gen
 * hand-off + DoP vote + instrument). audio()'s next frame cannot start until
 * mixer1 returns to inuint(c_audio), so the true worst case is max(BUSY,TAIL)
 * and BUSY+TAIL bounds the whole mixer loop. */
static SCPI_FPTR_CMD scpi_result_t mixer_tail_q(scpi_t *context)
{
    char buf[10];
    unsigned n = fmt_u32(csd_mb_get_mix_tail_peak(), buf, (unsigned)sizeof buf);
    if (n == 0) {
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
        return SCPI_RES_ERR;
    }
    SCPI_ResultCharacters(context, buf, n);
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t mixer_busy_q(scpi_t *context)
{
    char buf[10];
    unsigned n = fmt_u32(csd_mb_get_mix_busy_peak(), buf, (unsigned)sizeof buf);
    if (n == 0) {
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
        return SCPI_RES_ERR;
    }
    SCPI_ResultCharacters(context, buf, n);
    return SCPI_RES_OK;
}

/* MIXer:FRAMES? -- mixer1's sample-loop counter (csd_gen_mailbox.c).
 * The case-A ladder (factory +1.0 dB, production +5.8, mixer-no-gen +4..7,
 * mixer+gen +11.7) puts the artifact in our mixer-side work, but nothing so
 * far has PROVED mixer1 runs during a plain host stream. Read it either side
 * of a streamed-zero file: a delta of ~48k frames/s proves the loop ran;
 * zero means every mixer-path theory is void. */
static SCPI_FPTR_CMD scpi_result_t mixer_frames_q(scpi_t *context)
{
    char buf[10];
    unsigned n = fmt_u32(csd_mb_get_mix_frames(), buf, (unsigned)sizeof buf);
    if (n == 0) {
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
        return SCPI_RES_ERR;
    }
    SCPI_ResultCharacters(context, buf, n);
    return SCPI_RES_OK;
}

/* MIXer:DACPeak? -> peak|L|,peak|R| over the words handed to the DAC since
 * the last CLEAR (absolute values of the int32 samples; 2^31 = full scale).
 * MIXer:DACPeak:CLEAR arms the window. See csd_mb_note_dac_sample for why
 * this node, and not the USB capture mirror, is the only digital view of the
 * DAC-bound stream in a mixer build. */
static SCPI_FPTR_CMD scpi_result_t mixer_dacpeak_q(scpi_t *context)
{
    /* -> per node (0 host, 1 post-mix, 2 post-volume, 3 DAC hand-off):
     *    peakL, peakR, wordL, wordR   => 16 values.
     * Magnitudes alone are not evidence (an abs bug once made a frozen
     * negative word look like a pop); the raw word identifies the source:
     * ASCII-looking patterns = uninitialised static RAM, a stale buffer, or
     * real signal. */
    /* 7 nodes x (peakL,peakR,wordL,wordR) then nzL,nzR,firstFrame,wL,wR.
     * 5/6 bracket the DSP on the audio thread -- the only instrumented view
     * of the I2S-bound words in a MIXER build (csd_dsp.xc). Keep the count
     * in sync with CSD_DACNODES in csd_gen_mailbox.c: the rig parses the
     * fixed 33-value layout. */
    char buf[33 * 10 + 32];
    unsigned len = 0;
    unsigned nzc = 0, nzr = 0, wl = 0, wr = 0, ff = csd_mb_get_host_first_frame();
    csd_mb_get_host_nonzero(0u, &nzc, &wl);
    csd_mb_get_host_nonzero(1u, &nzr, &wr);
    for (unsigned node = 0; node < 7u; node++) {
        const unsigned v[4] = { csd_mb_get_dac_peak(0u, node),
                                csd_mb_get_dac_peak(1u, node),
                                csd_mb_get_dac_peak_word(0u, node),
                                csd_mb_get_dac_peak_word(1u, node) };
        for (unsigned i = 0; i < 4u; i++) {
            if (len) buf[len++] = ',';
            unsigned n = fmt_u32(v[i], buf + len, (unsigned)sizeof buf - len);
            if (n == 0) {
                SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
                return SCPI_RES_ERR;
            }
            len += n;
        }
    }
    {
        const unsigned tail[5] = { nzc, nzr, ff, wl, wr };
        for (unsigned i = 0; i < 5u; i++) {
            if (len) buf[len++] = ',';
            unsigned n = fmt_u32(tail[i], buf + len, (unsigned)sizeof buf - len);
            if (n == 0) {
                SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
                return SCPI_RES_ERR;
            }
            len += n;
        }
    }
    SCPI_ResultCharacters(context, buf, len);
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t mixer_dacpeak_clear(scpi_t *context)
{
    (void)context;
    csd_mb_clear_dac_peak();
    return SCPI_RES_OK;
}

/* DIAG:I2S? -> frames,bad,worstHi,worstLo,firstBad
 * LRCLK port-timestamp stats from the audio() task (budget = 32 ticks).
 * concurrent_fork.py proved the DAC-bound WORDS are silence while the mic
 * hears +25 dB on the first stream; if the timing delta is also clean, the
 * digital side of the board is fully exonerated and the case-A tone is
 * being generated inside the codec at stream open. */
static SCPI_FPTR_CMD scpi_result_t diag_i2s_q(scpi_t *context)
{
    char buf[5 * 10 + 4];
    unsigned f = 0, b = 0, hi = 0, lo = 0, fb = 0, len = 0;
    csd_mb_get_i2s(&f, &b, &hi, &lo, &fb);
    const unsigned v[5] = { f, b, hi, lo, fb };
    for (unsigned i = 0; i < 5u; i++) {
        if (i) buf[len++] = ',';
        unsigned n = fmt_u32(v[i], buf + len, (unsigned)sizeof buf - len);
        if (n == 0) {
            SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
            return SCPI_RES_ERR;
        }
        len += n;
    }
    SCPI_ResultCharacters(context, buf, len);
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t diag_i2s_clear(scpi_t *context)
{
    (void)context;
    csd_mb_clear_i2s();
    return SCPI_RES_OK;
}

/* GENerator:DOPpRObe? -> frames,matches,lastL,lastR
 *
 * Why the 4-vote DoP latch takes 27 509 frames on the MIXER artifact where
 * the production artifact latches in 238, and why the loopback witness of
 * the DoP payload is 20 dB quieter there (peak 0.026 vs 0.244). Candidates:
 * the mixer thread votes on different words than the production vote point
 * (negotiated sample format -- the rig streams S32_LE and plughw may
 * convert, or the >96 kHz half-frame exchange in mixer1), so MARK fails on
 * most frames; or the words are intact and something else starves the vote.
 * frames==0 means the hook never ran. matches==0 with frames>0 means the
 * mixer thread is not looking at marker bytes at all, which relocates the
 * bug. lastL/lastR show the actual words for hand-decoding.
 *
 * Diagnostic-only; the counters are monotones read individually.
 */
static SCPI_FPTR_CMD scpi_result_t gen_dop_probe_q(scpi_t *context)
{
    char buf[4 * 10 + 4];
    unsigned f = 0, m = 0, l = 0, rr = 0;
    unsigned len = 0;

    csd_mb_get_dopvote(&f, &m, &l, &rr);
    const unsigned v[4] = { f, m, l, rr };
    for (unsigned i = 0; i < 4u; i++) {
        if (i) buf[len++] = ',';
        unsigned n = fmt_u32(v[i], buf + len, (unsigned)sizeof buf - len);
        if (n == 0) {
            SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
            return SCPI_RES_ERR;
        }
        len += n;
    }
    SCPI_ResultCharacters(context, buf, len);
    return SCPI_RES_OK;
}

/* GENerator:STATe:REJect? -> last_reject_code,reject_count */
static SCPI_FPTR_CMD scpi_result_t gen_reject_q(scpi_t *context)
{
    /* Rejection codes are CSD_GEN_REJ_* (0..7), always non-negative, so
     * fmt_u32 is enough and the signed round trip through snprintf("%d")
     * that this replaces was risk with no benefit. */
    char buf[24];
    unsigned len = fmt_u32((uint32_t)(csd_gen_state_last_reject() < 0
                                      ? 0 : csd_gen_state_last_reject()),
                           buf, sizeof buf);
    if (len == 0) { SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
                    return SCPI_RES_ERR; }
    buf[len++] = ',';
    unsigned n = fmt_u32(csd_gen_state_rejects(), buf + len,
                         (unsigned)sizeof buf - len);
    if (n == 0) { SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
                  return SCPI_RES_ERR; }
    SCPI_ResultCharacters(context, buf, len + n);
    return SCPI_RES_OK;
}

#endif /* !SPECIALTYCIRCUITS_RELEASE: MIXer counters, DIAG:I2S, probes */

static const scpi_command_t csd_commands[] = {
    /* IEEE 488.2 mandated subset */
    { .pattern = "*IDN?",  .callback = q_idn     },   /* ours, not CoreIdnQ: see q_idn */
    { .pattern = "*CLS",   .callback = SCPI_CoreCls  },
    { .pattern = "*RST",   .callback = SCPI_CoreRst  },
    { .pattern = "*OPC?",  .callback = SCPI_CoreOpcQ },
    { .pattern = "*ESR?",  .callback = SCPI_CoreEsrQ },
    { .pattern = "*STB?",  .callback = SCPI_CoreStbQ },

    /* SCPI mandatory */
    { .pattern = "SYSTem:ERRor[:NEXT]?", .callback = SCPI_SystemErrorNextQ  },
    { .pattern = "SYSTem:ERRor:COUNt?",  .callback = SCPI_SystemErrorCountQ },
    { .pattern = "SYSTem:VERSion?",      .callback = SCPI_SystemVersionQ    },

    /* DSP preset: separate entries for the query and the set halves. Verified
     * necessary -- a bare pattern does NOT match the '?' form (see
     * cmd_is_query). No parameter placeholders in either pattern: libscpi
     * matches the pattern against the command HEADER only, so "<string>" would
     * be compared as a literal keyword and never match (that is what silently
     * broke preset/register commands -- the callbacks never ran and the host
     * saw -113). Parameters are read with SCPI_Param*() inside the callback. */
    { .pattern = "DIAG:DSP:PRESet?", .callback = q_dsp   },
    { .pattern = "DIAG:DSP:PRESet",  .callback = set_dsp },

    /* Unit identity. Queries only -- there is deliberately no setter and no
     * OTP write path. Patterns carry no parameter placeholders, same rule as
     * above: libscpi matches the pattern against the command HEADER only. */
    { .pattern = "DIAG:IDENTity:SERial?", .callback = q_ident_serial },
    { .pattern = "DIAG:IDENTity:SOURce?", .callback = q_ident_source },
    { .pattern = "DIAG:IDENTity:HASH?",   .callback = q_ident_hash   },
    { .pattern = "DIAG:IDENTity:RAW?",    .callback = q_ident_raw    },
    { .pattern = "DIAG:IDENTity:STAGe?",  .callback = q_ident_stage  },

    /* raw codec registers, via the single I2C writer on tile 0. Same rule: no
     * parameter placeholders in the pattern, parameters come from
     * SCPI_Param*() inside the callback. */
    { .pattern = "REGister:READ?",  .callback = reg_read_q },
    { .pattern = "REGister:WRITE",  .callback = reg_write  },

    /* Function generator. Set and query halves are separate entries, and no
     * pattern carries a parameter placeholder -- both rules established by
     * DIAG:DSP:PRESet above (libscpi matches the pattern against the command
     * HEADER only, so a placeholder is compared as a literal keyword and the
     * callback never runs). The <n>-suffix forms are the library's
     * abbreviated-mnemonic convention: GEN2:SINE works as well as
     * GENERATOR:SINE. */
    { .pattern = "GENerator:SINe",          .callback = gen_sine    },
    { .pattern = "GENerator:SQUare",        .callback = gen_square  },
    { .pattern = "GENerator:SWEep",         .callback = gen_sweep   },
    { .pattern = "GENerator:TONEs",         .callback = gen_tones   },
    { .pattern = "GENerator:NOISe",         .callback = gen_noise   },
    { .pattern = "GENerator:DC",            .callback = gen_dc      },
    { .pattern = "GENerator:OFF",           .callback = gen_off     },
    { .pattern = "GENerator:STATe?",        .callback = gen_state_q  },
    { .pattern = "MIXer:GAIN:HOST",         .callback = mixer_gain_host },
    { .pattern = "MIXer:GAIN:GENerator",    .callback = mixer_gain_gen  },
    { .pattern = "MIXer:GAIN?",             .callback = mixer_gain_q    },
    /* L/R trim (TODO 3+4): production calibration -- NOT under the debug
     * gate. Counts of 0.125 dB, 0 (max) down to -256 (-32 dB). */
    { .pattern = "VOLume:TRIM:LEFT",        .callback = vol_trim_left   },
    { .pattern = "VOLume:TRIM:RIGHT",       .callback = vol_trim_right  },
    { .pattern = "VOLume:TRIM?",            .callback = vol_trim_q      },
    { .pattern = "VOLume:TRIM:STATus?",     .callback = vol_trim_status_q },
    { .pattern = "VOLume:TRIM:LOAD",        .callback = vol_trim_load     },
#ifndef SPECIALTYCIRCUITS_RELEASE
    /* Debug-only rows (TODO item 5): gone in release builds together with
     * their callbacks above; a stray DEBUG: command then answers -113.
     * Renamed under the DEBUG: group (old names kept as comments; rigs
     * moved with them in the same commit). */
    { .pattern = "DEBUG:GENerator:COUNts?",  .callback = gen_counts_q },
    { .pattern = "DEBUG:MIXer:FRAMES?",      .callback = mixer_frames_q },
    { .pattern = "DEBUG:MIXer:BUSY?",        .callback = mixer_busy_q },
    { .pattern = "DEBUG:MIXer:TAIL?",        .callback = mixer_tail_q },
    { .pattern = "DEBUG:MIXer:HWCfg?",       .callback = mixer_hwcfg_q },
    { .pattern = "DEBUG:MIXer:DACPeak?",     .callback = mixer_dacpeak_q },
    { .pattern = "DEBUG:MIXer:DACPeak:CLEAR",.callback = mixer_dacpeak_clear },
    { .pattern = "DEBUG:I2S?",               .callback = diag_i2s_q },
    { .pattern = "DEBUG:I2S:CLEAR",          .callback = diag_i2s_clear },
    { .pattern = "DEBUG:GENerator:VOLume?",  .callback = gen_volume_q },
    { .pattern = "DEBUG:GENerator:DOPpRObe?",.callback = gen_dop_probe_q },
    { .pattern = "DEBUG:GENerator:STATe:REJect?", .callback = gen_reject_q },
#endif

    SCPI_CMD_LIST_END
};

static scpi_interface_t csd_interface = {
    .error   = SCPI_Error,
    .write   = SCPI_Write,
    .control = SCPI_Control,
    .flush   = SCPI_Flush,
    .reset   = SCPI_Reset,
};

/* ---- context ---------------------------------------------------------- */
#define INPUT_BUF_LEN   160
#define ERROR_QUEUE_N    16

static char         input_buf[INPUT_BUF_LEN];
static scpi_t       ctx;
static scpi_error_t err_queue[ERROR_QUEUE_N];
static unsigned     inited;

static void csd_scpi_init(void)
{
    SCPI_Init(&ctx, csd_commands, &csd_interface, NULL,
              CSD_IDN_VENDOR, CSD_IDN_MODEL, CSD_IDN_SERIAL, CSD_IDN_FIRMWARE,
              input_buf, INPUT_BUF_LEN, err_queue, ERROR_QUEUE_N);
    /* No SCPI_InitHeap(): it exists only when
     * USE_DEVICE_DEPENDENT_ERROR_INFORMATION is on, and that
     * path is what the xcore stack analyser rejects (see
     * push_hw_error and scpi_user_config.h). */
    inited = 1;
}

/* ---- command path: byte-at-a-time from the XC transport ----------------
 * The XC side owns nothing here; it calls begin/put/end. Keeping the command
 * buffer on this side is what removes the XC/C pointer-space problem entirely
 * (see the note in csd_diag.h). */
static char     cbuf[INPUT_BUF_LEN];
static unsigned clen;
static unsigned accepting;            /* between begin() and end() */

void csd_scpi_begin(void)
{
    if (!inited) csd_scpi_init();
    clen = 0;
    accepting = 1;
}

void csd_scpi_put(unsigned b)
{
    if (!accepting) return;                       /* padding / stray traffic */
    if (b >= 0x80u) return;                       /* not 7-bit data: ignore */
    if (clen >= INPUT_BUF_LEN - 1u) {             /* overflow is loud, not a
                                                   * silent truncation */
        accepting = 0;
        clen = 0;
        reply_len = 0;
        SCPI_ErrorPush(&ctx, SCPI_ERROR_INVALID_STRING_DATA);
        return;
    }
    cbuf[clen++] = (char)b;
}

void csd_scpi_end(void)
{
    if (!accepting) return;
    accepting = 0;
    if (clen == 0) return;                        /* empty line: no reply */
    cbuf[clen] = 0;
    reply_len = 0;
#ifdef CSD_DIAG_TRACE
    printf("DIAG: cmd='%s' len=%u\n", cbuf, clen);
#endif
    {
        scpi_bool_t r = SCPI_Parse(&ctx, cbuf, (int)clen);   /* fills the ring */
#ifdef CSD_DIAG_TRACE
        printf("DIAG: parse rc=%d wrote=%u ring=%u trunc=%u drops=%u\n",
               (int) r, reply_len, (unsigned)(rwr != rrd), reply_truncs,
               reply_drops);
#endif
    }
    clen = 0;
}

/* ---- counters ---------------------------------------------------------- */
unsigned csd_diag_reply_drops(void)  { return reply_drops; }
unsigned csd_diag_reply_truncs(void) { return reply_truncs; }
