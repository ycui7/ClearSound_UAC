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
