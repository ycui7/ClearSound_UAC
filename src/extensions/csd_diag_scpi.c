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
#include "csd_power_state.h"  /* csd_pw_host_frames: live-stream probe for
                               * the kernel-update adoption wait */
#include "csd_version.h"
#include "csd_identity.h"
#include "csd_gen.h"
#include "csd_power.h"          /* POWER:TIMEout, owner case 14 */
#include "csd_ups_block.h"  /* DIAG:UPSAMPLing, the DSP block engine behind it */
#include "csd_errlog.h"     /* DIAG:ERRLOG — boot-flash death forensics      */
/* L/R trim persistence (TODO 3) is reached through csd_diag.h's proxy calls,
 * never by including csd_trim_store.h: that header's entry points drive the
 * tile[0] SQI ports and may only be called by the tile[0] codec server. */

/* *IDN? identity. These are the product's actual names, and they match the USB
 * descriptor's VENDOR_STR / PRODUCT_STR_A2 so that lsusb and the MIDI identity
 * cannot disagree about what the unit is (csd_identity.c:81 exists because that
 * inconsistency was a real defect here). No invented product code: an earlier
 * revision put "CSD-22" here, which was a firmware-only label that appears
 * nowhere on the product. */
#define CSD_IDN_VENDOR   "SpecialtyCircuits LLC"
#define CSD_IDN_MODEL    "ClearSound DAC"
/* Serial is not a constant any more, so it is not passed through SCPI_Init
 * at all: q_idn reads it live. See q_idn for why capture-at-init would be
 * wrong. This macro only fills the now-unused idn3 slot. */
#define CSD_IDN_SERIAL   ""
#define CSD_IDN_FIRMWARE CSD_FW_VERSION

/* ---- output plumbing -------------------------------------------------- */
/* 2026-11-10: 320 -> 640 -> 1024 -> 32768. WHY the last step: the kernel
 * container query (DIAG:DSP:COEFFicient?) must echo up to
 * (12 + CSD_KERNEL_MAX_TAPS*4) bytes as base64 -- ~21.5 KB. A truncated
 * kernel reply is CATASTROPHIC (it decodes as a plausible shorter kernel),
 * so this cap is sized from the container, not from taste: it now equals
 * the ring capacity minus framing. A reply that still cannot fit must fail
 * LOUDLY at the handler (see coef_q's pre-flight), never truncate.
 * REVERT history: 320 pre-2026-11-10, 640 for the INFO? tail, 1024 after. */
#define REPLY_MAX 32768

/* Receive side (csd_scpi_put's command buffer, see the parser section at
 * the file bottom): the binding receive limit for a SET is THIS buffer --
 * the MIDI path is event-at-a-time (csd_diag_midi.xc rx_event feeds
 * csd_scpi_put byte by byte, no message cap) and libscpi lexes in place
 * (parser.c assigns lex_state.buffer = the caller's buffer; its only
 * internal fifo is the error queue). Sized so the largest container
 * command fits in one message: header text + max_b64_chars (21872) plus
 * full headroom. Overflow stays LOUD (csd_scpi_put rejects with -151), it
 * never silently truncates. */
#define INPUT_BUF_LEN 33792

static unsigned reply_len;      /* bytes emitted by the current command */
static unsigned reply_truncs;

/* Reply ring, owned here (next to the parser) and pulled byte-by-byte by the
 * XC transport. Overflow drops and counts -- it never blocks, because the
 * consumer is a core that must keep answering the USB endpoint. Sized with
 * REPLY_MAX above so a maximum-size kernel reply fits in one command
 * (kernel staging on tile[1]: ring + input buffer + container staging; the
 * audio path shares neither stack nor budget with them). */
#define REPLY_BYTES 34816
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

/* Signed decimal: one leading '-', magnitude via fmt_u32. The magnitude is
 * negated in int64 before the cast so INT32_MIN survives (the vol_trim_q
 * inline pattern, factored out for the two COEFFicient replies).
 * NUL-terminates, returns the digit count, 0 if it would not fit. */
static unsigned fmt_s32(int32_t v, char *out, unsigned cap)
{
    if (v < 0 && cap >= 2u) {
        out[0] = '-';
        unsigned n = fmt_u32((uint32_t)(-(int64_t)v), out + 1, cap - 1u);
        if (n == 0) { out[0] = 0; return 0; }
        out[1 + n] = 0;
        return n + 1u;
    }
    return fmt_u32((uint32_t)v, out, cap);
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

/* FIR preset (owner request 2026-11-16). Reported from the tile[0] OWNER's
 * state of record (s_filter_bits), NOT a codec read: the ES9219Q I2C read
 * path on this board answers 0x00 for EVERY register -- CHIP_ID reads 0
 * (measured 2026-11-16, both bench units) -- so any codec-read status was
 * fiction, including the pre-funnel REGister:READ? "verifications".
 * TOGgle rides the same command the two-button hold uses. */
static SCPI_FPTR_CMD scpi_result_t q_filter_state(scpi_t *context)
{
    const char *s = (csd_diag_filter_state() == 6u) ? "HYBRID" : "LINEAR";
    SCPI_ResultCharacters(context, s, strlen(s));
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t cmd_filter_toggle(scpi_t *context)
{
    (void) context;
    csd_diag_filter_toggle();
    return SCPI_RES_OK;
}

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
static SCPI_FPTR_CMD scpi_result_t set_dsp(scpi_t *context);   /* below */

/* ---- DIAG:DSP:COEFFicient -- base64 kernel-container aperture ----------
 * (2026-11-10 owner directives). THE BLOCK WRITE IS THE ONLY WRITE PATH.
 * One command carries one whole kernel and applies it in ONE atomic
 * adoption; there is no per-index decimal set and no five-comma form -- a
 * caller cannot compose a kernel non-atomically (a half-loaded filter is
 * worse than a refused one: reject, never truncate, never zero-pad).
 *
 * Container (inside base64, little-endian): header id:u32, taps:u32,
 * width:u16, q_shift:u16 (12 bytes) + taps * width/8 coefficient bytes.
 * Layout constants and every limit come from the target table in
 * csd_dsp_bq.c through the getters -- FORMat?, the validator and the store
 * cannot drift. A host that sees an unknown magic or version in the
 * FORMat? reply must BAIL, not guess: never send what you cannot parse.
 *
 * Set:     DIAG:DSP:COEFFicient "<base64>"
 *          REJECTED with -224 ILLEGAL_PARAMETER_VALUE, table untouched,
 *          for: invalid base64, unpadded/garbage tail, trailing extra
 *          parameters, header shorter than 12 bytes, unknown id, taps that
 *          does not EQUAL the addressed target's capacity (biquad exactly
 *          5; FIR exactly CSD_KERNEL_FIR_TAPS -- one tap short is refused
 *          whole), width != 32, q != 30, or total decoded length !=
 *          12 + taps*4.
 * Query:   DIAG:DSP:COEFFicient?              -> the whole live container
 *          as base64 (request-side truth, exactly like PRESet? reports the
 *          posted request; task-applied truth is INFO? eq_kernel_src).
 *          Pre-flight: if the encoded reply cannot fit the reply ring the
 *          command FAILS LOUDLY (-200) and answers nothing -- a truncated
 *          kernel would look like a valid shorter one.
 *          DIAG:DSP:COEFFicient? <idx>        -> DEBUG/QUERY-ONLY per-word
 *          decimal readback (kept for humans; there is no set form).
 * Limits:  DIAG:DSP:COEFFicient:FORMat?       -> derived descriptor, magic
 *          and version FIRST. A host can write a correct encoder from this
 *          reply alone.
 * Restore: DIAG:DSP:COEFFicient:DEFault  (no reply; errors via the queue)
 *
 * Every accepted write, the PRESet write, and :DEFault run the same
 * sequence: codec soft-ramp DOWN (ES9219Q chip ramp, Reg 6 rate 0, Reg 7
 * mute -- Reg 3/15/16 volume registers untouched, so the PREVIOUS level is
 * what comes back), post the request, wait for the EQ task's one adoption
 * (witness eq_cfg_adopt), soft-ramp UP. The EQ task itself runs
 * pause/load/resume around every adoption (csd_dsp.xc); the software-LRCLK
 * loop pays nothing.
 *
 * Patterns carry no parameter placeholders -- libscpi matches the pattern
 * against the command HEADER only (see set_dsp's block note), and
 * matchCommand fails a row whose pattern is exhausted while the header
 * still has text, so the rows cannot shadow each other. */

/* Kernel staging on tile[1], off any stack: receive payload and query
 * payload (never simultaneously live -- the diagnostic thread is
 * single-threaded). Sized to the contract maximum the validator accepts
 * (FIR_TAPS == MAX_TAPS): kstage holds the largest legal decoded payload,
 * kb64 its base64 echo (5,480 chars). aligned(4): coef words read as int32. */
#define KSTAGE_BYTES (CSD_KERNEL_HDR_BYTES + CSD_KERNEL_MAX_TAPS * 4)
/* Tail-most DP bucket with the kernel stores: at 1024 capacity these are
 * ~9.5 KB of the diagnostic tile's data, and cold (one command at a time).
 * Same layout-protection rule as csd_dsp_store.c -- see the comment there.
 * kstage is sized to the WORST-CASE decoded size of a legal base64 length
 * ((bytes+2)/3*3 = 4110 for the 4108-byte contract maximum): the pre-decode
 * guard compares len/4*3 against sizeof kstage, and with the buffer at
 * exactly KSTAGE_BYTES that guard rejected the contract's OWN largest legal
 * container (4110 > 4108) -- measured on the bench, -224 with the table
 * untouched. The validator still demands decoded length == 12 + taps*4
 * exactly, so the slack bytes are unreachable. */
static unsigned char kstage[(KSTAGE_BYTES + 2u) / 3u * 3u]
    __attribute__((section(".dp.bss.large"), aligned(4)));
static char kb64[(KSTAGE_BYTES + 2) / 3 * 4 + 8]
    __attribute__((section(".dp.bss.large")));

/* Witnesses (INFO? 91..94; unconditional definitions, the SCPI file does
 * not receive CSD_EXTRA_FLAGS): task pause (csd_dsp.xc), adoption
 * (csd_eq_ring.c), owner-side ramp counter mirrored from the tile[0] reply,
 * adoption waits that timed out. */
extern unsigned g_csd_eq_cfg_adopt;
extern unsigned g_csd_eq_pause_events, g_csd_eq_pause_ticks_max;
unsigned g_csd_krnl_ramp_events;
unsigned g_csd_krnl_adopt_timeouts;

/* Live-stream probe: is a host stream actually being delivered right now?
 * Two independent per-frame counters, OR'd:
 *   g_csd_ubm_calls  (csd_dsp.xc) -- one per UserBufferManagement call, i.e.
 *                    once per frame at the stream rate, content-blind.
 *   csd_pw_host_frames() (csd_power_state.c) -- one per host sample at the
 *                    same hook entry, also content-blind (the NON-ZERO gate
 *                    belongs to its sibling csd_pw_host_activity, NOT to this
 *                    counter -- checked in the source, because the first
 *                    version of this comment claimed the opposite).
 * WHY two: each is compiled in under a different build flag (CSD_EQ_DIAG_WITNESS
 * and CSD_IDLE_POWER). A liveness probe that vanishes with a diagnostic flag is
 * a probe that silently reports "no stream" and skips the adoption wait -- the
 * exact success-without-adoption failure this whole sequence exists to prevent.
 * Neither costs anything here: both already run per frame on the audio core.
 * A bounded spin is fine on this core -- it is the diagnostic thread, not the
 * audio path -- and the loop only gates the adoption wait. */
extern unsigned g_csd_ubm_calls;        /* unconditional global, csd_dsp.xc */
extern unsigned g_csd_eq_stalls;        /* INFO? 105 (11.11.31 ring waits)  */
extern unsigned g_csd_feed_writes;      /* INFO? field 98 (csd_dsp.xc feed) */
extern unsigned csd_feed_owner;         /* INFO? field 99 (gate snapshot)   */
static unsigned krnl_stream_live(void)
{
    const unsigned f0 = g_csd_ubm_calls;
    const unsigned p0 = csd_pw_host_frames();
    for (unsigned i = 0; i < 50000u; i++) {}
    return g_csd_ubm_calls != f0 || csd_pw_host_frames() != p0;
}

/* The atomic update sequence (owner directive 2026-11-10): ramp down with
 * the CHIP's own soft ramp (codec-server owner waits the ramp out), post
 * the request, wait for the EQ task's single adoption when a stream is
 * live, ramp back up. post() moves exactly one complete request.
 *
 * Returns 0 iff a LIVE stream did not observe the adoption (timeout): the
 * codec is still ramped back up (audio must never be left muted by a failed
 * write) but the caller must answer with an error -- a set-command that
 * returns success while g_csd_eq_cfg_adopt never moved is the
 * success-with-delta-adopt-0 lie the owner banned (2026-11-10). With no
 * stream the EQ task has nothing to adopt (the engine is not engaged; the
 * pending config is adopted at stream start and witnessed there), so the
 * wait -- and the requirement -- apply only when live.
 *
 * CSD_KRNL_FORCE_ADOPT_TIMEOUT is a BENCH-ONLY define (never in a shipping
 * build; build_tree.sh does not pass it): it collapses the wait bound to one
 * iteration so the adoption can never be observed. That is the only way to
 * prove the -300 path is live code rather than a comment, and to prove the
 * codec is NOT left muted when it fires. */
#ifdef CSD_KRNL_FORCE_ADOPT_TIMEOUT
#define KRNL_ADOPT_SPIN 1u
#else
#define KRNL_ADOPT_SPIN 4000000u
#endif
static int krnl_update_seq(void (*post)(void *), void *arg)
{
    g_csd_krnl_ramp_events = csd_diag_krnl_seq(0u);
    const unsigned a0 = g_csd_eq_cfg_adopt;
    post(arg);
    int ok = 1;
    if (krnl_stream_live()) {
        for (unsigned i = 0; i < KRNL_ADOPT_SPIN && g_csd_eq_cfg_adopt == a0; i++) {}
        if (g_csd_eq_cfg_adopt == a0) { g_csd_krnl_adopt_timeouts++; ok = 0; }
    }
    g_csd_krnl_ramp_events = csd_diag_krnl_seq(1u);   /* never leave muted */
    return ok;
}

/* Two SCPI codes this file owns (2026-11-10). They are numeric because
 * libscpi's FULL error table is compiled out here: scpi_user_config.h pins
 * USE_FULL_ERROR_LIST to 0 (the XE rows cost flash), and the XE enumerators
 * -- SCPI_ERROR_DATA_OUT_OF_RANGE, SCPI_ERROR_DEVICE_ERROR -- are generated
 * only from that table, so naming them is a compile error (measured:
 * "use of undeclared identifier 'SCPI_ERROR_DEVICE_ERROR'"). The codes are
 * from the SCPI/IEEE 488.2 set, so they are stable regardless of the table,
 * and libscpi still maps them to the right *ESR bits (-200..-299 -> EER,
 * -300..-399 -> DER). Their Translate() text is "Unknown error" with the
 * table trimmed, which is exactly why both are documented in
 * DIAG:DSP:COEFFicient:FORMat? and in docs, and why the codes -- not the
 * text -- are the contract.
 *   -222 Data out of range: a well-formed kernel that is longer than the
 *      measured per-rate ceiling -- valid container, wrong rate. Shorten it
 *      or lower the sample rate (see csd_dsp_kernel_rate_limit).
 *   -300 Device-specific error: the request ran but the device could not
 *      complete it -- here, a live stream never observed the kernel adoption
 *      (kernel_gate_runner / t2_write_matrix read eq_krnl_adopt_timeouts to
 *      confirm it). NOT libscpi's -200 EXECUTION_ERROR, which this file
 *      already uses for reply-size refusals: two failures must not share one
 *      code. */
#define CSD_ERR_DATA_OUT_OF_RANGE   (-222)
#define CSD_ERR_ADOPT_TIMEOUT       (-300)

static unsigned le32_at(const unsigned char *b)
{
    return (unsigned)b[0] | ((unsigned)b[1] << 8) |
           ((unsigned)b[2] << 16) | ((unsigned)b[3] << 24);
}
static unsigned le16_at(const unsigned char *b)
{
    return (unsigned)b[0] | ((unsigned)b[1] << 8);
}

struct kernel_post { unsigned id; unsigned taps; };
static void kernel_post_cb(void *a)
{
    struct kernel_post *k = (struct kernel_post *)a;
    csd_dsp_kernel_publish(k->id, k->taps,
                           (const int32_t *)(const void *)(kstage + CSD_KERNEL_HDR_BYTES));
}

static SCPI_FPTR_CMD scpi_result_t coef_set(scpi_t *context)
{
    const char *p;
    size_t len;

    if (!SCPI_ParamCharacters(context, &p, &len, TRUE)) return SCPI_RES_ERR;
    /* Exactly one parameter: a trailing second parameter is -224, not a
     * hint. (The libscpi lexer DROPS a space-separated second parameter
     * before the callback runs -- documented on this device -- so comma
     * junk is what actually reaches here; both ways end in rejection.) */
    {
        char junk[8];
        if (param_text(context, junk, sizeof junk, FALSE)) {
            SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
            return SCPI_RES_ERR;
        }
    }
    if (len == 0 || (len % 4u) != 0u || len / 4u * 3u > sizeof kstage) {
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
    const int n = b64_decode(p, len, kstage, sizeof kstage);
    if (n < (int)CSD_KERNEL_HDR_BYTES) {       /* bad base64 or short header */
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
    const unsigned id    = le32_at(kstage);
    const unsigned taps  = le32_at(kstage + 4);
    const unsigned width = le16_at(kstage + 8);
    const unsigned q     = le16_at(kstage + 10);
    const unsigned cap   = csd_dsp_kernel_max_taps_for(id);
    if (cap == 0u || taps == 0u || taps > cap ||
        (id == CSD_KERNEL_ID_BIQUAD && taps != 5u) ||   /* structural: 5 words */
        width != CSD_KERNEL_WIDTH_BITS || q != CSD_KERNEL_Q_SHIFT ||
        (unsigned)n != CSD_KERNEL_HDR_BYTES + taps * 4u) {
        /* unknown target, no coefficients, a container larger than the
         * physical store, wrong width or q, or a length that disagrees with
         * the header. taps == 0 and taps > cap are load-bearing (2026-11-10):
         * the store used to accept 1..cap here and then
         * csd_dsp_bq_config silently refused adoption (it required
         * tp == max_taps_for(id)) -- a SHORT container was a
         * success-with-delta-adopt-0 lie. Both gates now run the SAME
         * predicate, csd_dsp_kernel_taps_legal(), so no container can be
         * accepted here and refused there; the rejection below stays loud with
         * -224 and the table stays byte-identical. */
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
    if (!csd_dsp_kernel_taps_legal(id, taps)) {
        /* Well-formed, but longer than the CURRENT stream rate can compute:
         * the measured per-rate ceiling (csd_dsp.h) is below the physical
         * capacity. Distinct from -224 on purpose -- a host that gets -222 has
         * a valid container and must either shorten it or lower the sample
         * rate; FORMat? names the limit and the rate it belongs to. */
        SCPI_ErrorPush(context, CSD_ERR_DATA_OUT_OF_RANGE);
        return SCPI_RES_ERR;
    }
    struct kernel_post kp = { id, taps };
    if (!krnl_update_seq(kernel_post_cb, &kp)) {
        /* A live stream never saw the adoption: report it, do not let the
         * silent-success contract hide an unapplied kernel. */
        SCPI_ErrorPush(context, CSD_ERR_ADOPT_TIMEOUT);
        return SCPI_RES_ERR;
    }
    return SCPI_RES_OK;   /* silent by contract; readback confirms */
}

static SCPI_FPTR_CMD scpi_result_t coef_q(scpi_t *context)
{
    /* Indexed debug/query-only form first: COEFFicient? <idx>. */
    char txt[16];
    if (param_text(context, txt, sizeof txt, FALSE)) {
        uint32_t idx;
        if (!parse_num(txt, strlen(txt), &idx)) {
            /* same contract as PRESet: a non-numeric index is a refused
             * value (-224), not a transport fault. */
            SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
            return SCPI_RES_ERR;
        }
        if (idx >= csd_dsp_kernel_request_taps()) {
            SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
            return SCPI_RES_ERR;
        }
        char buf[16];
        unsigned n = fmt_s32(csd_dsp_bq_coef_read(idx), buf, sizeof buf);
        if (n == 0) { SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
                      return SCPI_RES_ERR; }
        SCPI_ResultCharacters(context, buf, n);
        return SCPI_RES_OK;
    }

    /* Whole-kernel container query: header + request-side coefficients. */
    const unsigned en   = csd_dsp_kernel_request_en();
    const unsigned id   = csd_dsp_kernel_request_id();
    const unsigned taps = csd_dsp_kernel_request_taps();
    if (taps > CSD_KERNEL_MAX_TAPS) {          /* cannot happen; belt */
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
        return SCPI_RES_ERR;
    }
    /* Build the payload in the staging buffer, then encode. */
    unsigned char *h = kstage;
    h[0] = (unsigned char)id;        h[1] = (unsigned char)(id >> 8);
    h[2] = (unsigned char)(id >> 16); h[3] = (unsigned char)(id >> 24);
    h[4] = (unsigned char)taps;      h[5] = (unsigned char)(taps >> 8);
    h[6] = (unsigned char)(taps >> 16); h[7] = (unsigned char)(taps >> 24);
    h[8] = (unsigned char)CSD_KERNEL_WIDTH_BITS; h[9] = 0;
    h[10] = (unsigned char)CSD_KERNEL_Q_SHIFT;   h[11] = 0;
    int32_t *w = (int32_t *)(void *)(kstage + CSD_KERNEL_HDR_BYTES);
    for (unsigned i = 0; i < taps; i++) w[i] = csd_dsp_bq_coef_read(i);
    (void)en;                                   /* readback already defaults-aware */
    const unsigned total = CSD_KERNEL_HDR_BYTES + taps * 4u;
    const size_t need = ((total + 2u) / 3u) * 4u;
    /* LOUD failure, never a truncated kernel reply: an echo that decodes
     * as a shorter kernel is worse than no answer. */
    if (need > sizeof kb64 || need > (size_t)REPLY_MAX - 8u ||
        need > (size_t)REPLY_BYTES - 16u) {
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
        return SCPI_RES_ERR;
    }
    const size_t len = b64_encode(kstage, total, kb64, sizeof kb64);
    if (len == 0) { SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
                    return SCPI_RES_ERR; }
    SCPI_ResultCharacters(context, kb64, (int)len);
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t coef_format_q(scpi_t *context)
{
    /* Derived ONLY from the target table and the buffer constants the
     * parser itself checks -- a host can size its encoder from this reply
     * alone. Magic + version FIRST. KERN2: container with in-payload
     * header (KERN1's fixed 5-int32 shape was retired same day; a host
     * that sees anything else must bail, not guess). */
    static const char pre1[] = "KERN2,1,base64,le,int32,signed,unit=taps,"
                               "header=id:u32,taps:u32,width:u16,q:u16,"
                               "max_taps=";
    static const char mid1[] = ",container_taps=";
    static const char mid2[] = ",max_payload_bytes=";
    static const char mid3[] = ",max_b64_chars=";
    static const char mid4[] = ",targets=1:biquad:5|2:fir:";
    static const char mid5[] = ",recv_cmd_limit=";
    static const char mid6[] = ",reply_limit=";
    /* Per-rate ceiling (2026-11-10): what the aperture will ACCEPT for the
     * rate the stream is running at now, which rate that is, and the whole
     * measured tier table. ';' inside the table so a comma-splitting host
     * parser still sees one descriptor field per key. */
    static const char mid7[] = ",fir_taps_allowed=";
    static const char mid8[] = ",rate_hz=";
    static const char mid9[] = ",fir_taps_by_rate=48000:";
    static const char midA[] = "|96000:";
    static const char midB[] = "|192000:";
    static const char midC[] = "|384000:";
    static char out[400];
    unsigned o = 0;
    const unsigned fir   = csd_dsp_kernel_max_taps_for(CSD_KERNEL_ID_FIR);
    const unsigned cont  = csd_dsp_kernel_container_max_taps();
    const unsigned payld = CSD_KERNEL_HDR_BYTES + fir * 4u;
    const unsigned b64c  = ((payld + 2u) / 3u) * 4u;
    unsigned n;
#define KAPP(s) do { for (const char *q_ = (s); *q_; q_++) out[o++] = *q_; } while (0)
#define KNUM(v) do { n = fmt_u32((v), out + o, sizeof out - o - 1u); o += n; } while (0)
    KAPP(pre1); KNUM(fir);
    KAPP(mid1); KNUM(cont);
    KAPP(mid2); KNUM(payld);
    KAPP(mid3); KNUM(b64c);
    KAPP(mid4); KNUM(fir);
    KAPP(mid5); KNUM(INPUT_BUF_LEN - 1u);
    KAPP(mid6); KNUM(REPLY_MAX);
    KAPP(mid7); KNUM(csd_dsp_kernel_rate_limit(CSD_KERNEL_ID_FIR));
    KAPP(mid8); KNUM(csd_dsp_kernel_rate_limit_rate_hz());
    KAPP(mid9); KNUM(CSD_KERNEL_FIR_TAPS_AT_48);
    KAPP(midA); KNUM(CSD_KERNEL_FIR_TAPS_AT_96);
    KAPP(midB); KNUM(CSD_KERNEL_FIR_TAPS_AT_192);
    KAPP(midC); KNUM(CSD_KERNEL_FIR_TAPS_AT_384);
#undef KAPP
#undef KNUM
    out[o] = 0;
    SCPI_ResultCharacters(context, out, (int)o);
    return SCPI_RES_OK;
}

static void def_post(void *a) { (void)a; csd_dsp_bq_coef_restore(); }

/* ---- DEBUG:DSP:CLEAR -- witness window control (2026-11-10) --------------
 * The per-frame tick witnesses are MAXES since boot, so a per-configuration
 * measurement of eq_ticks_max is impossible without a reset: measured the
 * scalar FIR at 16..256 taps across four host rates, every reading after the
 * first would have been the maximum of everything run before it. This clears
 * the level-type witnesses (maxes, high waters) and answers with the value it
 * replaced, so the clear is VERIFIED by the reply rather than assumed. The
 * cumulative counters (frames, adopts, calls) are left alone -- callers take
 * deltas of those, which is what an absolute counter is for.
 * REVERT: delete this handler and its row; the tap table then has no
 * per-configuration evidence behind it. */
static SCPI_FPTR_CMD scpi_result_t dsp_clear(scpi_t *context)
{
    extern unsigned g_csd_eq_ticks_max, g_csd_eq_pause_ticks_max;
    extern unsigned g_csd_eq_occ_high, g_csd_eq_underrun, g_csd_eq_overrun;
    extern unsigned g_csd_lrclk_ts_min, g_csd_lrclk_ts_max;
    char buf[32];
    const unsigned pt = g_csd_eq_ticks_max, pp = g_csd_eq_pause_ticks_max;
    g_csd_eq_ticks_max = 0u;
    g_csd_eq_pause_ticks_max = 0u;
    g_csd_eq_occ_high = 0u;
    g_csd_eq_underrun = 0u;
    g_csd_eq_overrun = 0u;
    g_csd_lrclk_ts_min = 0xFFFFFFFFu;
    g_csd_lrclk_ts_max = 0u;
    const int n = snprintf(buf, sizeof buf, "%u,%u", pt, pp);
    if (n > 0) SCPI_ResultCharacters(context, buf, (int)(n < (int)sizeof buf ? n : sizeof buf - 1));
    return SCPI_RES_OK;
}

/* ---- DEBUG:REPLY? -- the transport's own loss counters (2026-11-10) ------
 * WHY: a DIAG:UPSAMPLing:INFO? came back with 83 of 95 fields while every
 * witness delta computed from it was garbage. The client, the format string
 * and the ring had to be told apart, and only the device's own counters can
 * do it: reply_drops counts bytes the producer threw away because the ring
 * was full (device-side loss), reply_truncs counts replies capped by
 * REPLY_MAX. Both zero across a short reply means the bytes left the device
 * whole and the loss is downstream -- which is what the bench measured.
 * REVERT: delete this handler and its row. */
/* REVERT(dp182): INFOLEN? -- snprintf bookkeeping for the INFO? reply.
 * WHY: device emits 134 of 147 compiled fields; need the raw snprintf return
 * values (wanted-bytes, not written-bytes) and the final n handed to
 * SCPI_ResultCharacters to tell format truncation from transport truncation. */
static unsigned g_csd_infolen_n1, g_csd_infolen_n2, g_csd_infolen_final;
static SCPI_FPTR_CMD infolen_q(scpi_t *context)
{
    char b[48];
    unsigned k = (unsigned)snprintf(b, sizeof b, "%u,%u,%u",
                    g_csd_infolen_n1, g_csd_infolen_n2, g_csd_infolen_final);
    SCPI_ResultCharacters(context, b, (int)k);
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t reply_stats_q(scpi_t *context)
{
    char buf[64];
    const unsigned n = snprintf(buf, sizeof buf, "%u,%u,%u,%u",
                                reply_len, reply_drops, reply_truncs,
                                (unsigned)(rwr != rrd));
    if (n > 0) {
        SCPI_ResultCharacters(context, buf, (int)(n < sizeof buf ? n : sizeof buf - 1));
    }
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t coef_default(scpi_t *context)
{
    if (!krnl_update_seq(def_post, 0)) {   /* SHIPPED DEFAULT: unity, not
                                              the shelf */
        SCPI_ErrorPush(context, CSD_ERR_ADOPT_TIMEOUT);
        return SCPI_RES_ERR;
    }
    return SCPI_RES_OK;
}

/* DIAG:DSP:PRESet <n> -- the set half, routed through the SAME
 * soft-ramp/pause/load/adopt/restore sequence as the kernel block write
 * (owner directive 2026-11-10: mid-stream configuration changes must be
 * safe, and the PRESet adoption is the path that demonstrably corrupted).
 * The query half stays q_dsp(), registered as its own entry: libscpi
 * matches a pattern against the command HEADER only, and a bare pattern
 * does not match the '?' form (see the block note above). */
struct preset_post { unsigned v; };
static void preset_post_cb(void *a)
{
    struct preset_post *p = (struct preset_post *)a;
    csd_dsp_set_preset(p->v);
    /* Volatility contract (2026-11-10): any PRESet write returns the engine
     * to the SHIPPED DEFAULT kernel -- unity/identity, not any loaded
     * kernel -- so a tester-composed kernel can never survive into a preset
     * change. See the aperture block in csd_dsp_bq.c. */
    csd_dsp_bq_coef_restore();
}

static SCPI_FPTR_CMD scpi_result_t set_dsp(scpi_t *context)
{
    char txt[16];
    uint32_t v;

    if (!param_text(context, txt, sizeof txt, TRUE)) return SCPI_RES_ERR;
    if (!parse_num(txt, strlen(txt), &v)) {
        /* Contract (2026-11-10): a non-numeric parameter is -224
         * ILLEGAL_PARAMETER_VALUE, the same code the container validator
         * uses for malformed input -- not libscpi's default -151/-104
         * "invalid string data". A typo'd PRESet is a refused value. */
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
    /* 0 OFF (byte-exact), 1 delay-only, 2 EQ -- see csd_dsp.h. The
     * upper bound matters: csd_dsp_set_preset() treats any value >= 2 as
     * "FIR preset", so a typo'd 7 would silently select a kernel. */
    if (v > 2u) {
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
#if defined(CSD_RELEASE) && CSD_RELEASE
    /* RELEASE image: the DSP path is pinned (preset 0, engine unarmed in
     * csd_ups_block.c / csd_dsp.xc), so there is nothing to install a preset
     * into. The command is ACCEPTED -- a host script must not have to know
     * the build -- and discarded; DIAG:DSP:PRESet? keeps answering 0, which
     * is the truth here. Nothing else in the DSP's own code is stubbed: its
     * per-frame body is the certified byte-exact bypass. */
    return SCPI_RES_OK;
#endif
    struct preset_post pp = { v };
    if (!krnl_update_seq(preset_post_cb, &pp)) {
        SCPI_ErrorPush(context, CSD_ERR_ADOPT_TIMEOUT);
        return SCPI_RES_ERR;
    }
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
        case CSD_ID_FLASH:   s = "FLASH";   break;   /* factory die serial     */
        case CSD_ID_OTP:     s = "OTP";     break;   /* retired; never set     */
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
 * (csd_trim_store.h -- this thread must not touch flash itself). The trim is
 * applied in the CODEC (MASTER TRIM regs 17..20 plus per-channel regs 15/16,
 * written by vol_apply_digital), NOT in the mixer: owner directive 2026-11-04
 * says the mixer handles no trim, and csd_mix_apply_trim() is an identity
 * pass-through. Application point the directive names: host SET_CUR and button
 * paths alike.
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
    /* Tile note (verified by reading the code 2026-11-04, after a detour
     * that added -- then removed -- a tile[0] publish): csd_mb_trim's only
     * readers are this task (VOLume:TRIM?/setter) and the boot/rate writer
     * in csd_AudioHwInit, and BOTH run on tile[1] -- AudioHwInit() is called
     * from the audio() task (lib_xua xua_audiohub.xc:905) and audio() lives
     * on AUDIO_IO_TILE=1. One tile, one copy, one writer. No cross-tile
     * seeding is needed; an earlier revision believed AudioHwInit was on
     * tile[0] and published there -- that copy had no reader. */
    /* Write MASTER TRIM NOW, not on some later pass (directive 2026-11-04:
     * trim is register setup; the register write is the effect). Four
     * writes through the existing tile[0] proxy -- same chanend discipline
     * as REGister:WRITE, blocking on THIS core (not the audio path), so a
     * rig reads the acoustic effect immediately. Order: registers first,
     * journal second, so flash latency can never delay the audible value;
     * if the journal fails the error queue says so while the trim stands,
     * same contract as before. Value math shared with the boot writer via
     * csd_trim_master_trim_q31 / csd_trim_reg_byte -- one implementation. */
    {
        unsigned q31 = csd_trim_master_trim_q31(packed);
        for (unsigned ti = 0; ti < 4u; ti++) {
            unsigned tb = csd_trim_reg_byte(q31, ti);
            if (!csd_diag_reg_write(CSD_TRIM_REG_BASE + ti, tb)) {
                SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
                break;
            }
        }
    }
    /* Journal through the tile[0] server (csd_diag_regs.xc) -- this core
     * must not touch the SQI ports. Synchronous by design: the reply means
     * the value is in flash (or the status says why not), so a rig can read
     * VOLume:TRIM:STATus? immediately after this returns and believe it.
     * A rejected journal is NOT silently swallowed -- the trim still stands
     * for this power cycle, and the error queue reports that it did not
     * persist, so "set but not saved" can never look like "saved". */
    if (!csd_diag_trim_save(packed)) {
        /* libscpi has no hardware-failure code; EXECUTION_ERROR is what this
         * file already uses for an operation that did not happen. The rig
         * reads SYST:ERR? to tell "saved" from "set but not journalled". */
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
    }
    /* Actuate the per-channel term. Ordering matters: the journal above is what
     * publishes the pair to tile[0], and the applier reads it from there. Ask
     * before journal and the registers get the PREVIOUS trim. This is the only
     * way regs 15/16 move on a trim set -- writing them from here instead would
     * put a second writer on a register the tile[0] applier owns and dedupes. */
    csd_diag_vol_reapply();
    return SCPI_RES_OK;
bad:
    SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
    (void)why;
    return SCPI_RES_ERR;
}

/* ---- PERSISTence:SAVE -- owner case 4 --------------------------------------
 * The journal's WRITE side existed (every VOLume:TRIM set journalled) but there
 * was no way for a rig to ask for a save explicitly, so case 4 could not prove
 * that a written pair reaches QSPI and survives a LOAD -- the suite reported it
 * as TODO naming this exact gap.
 *
 * It takes no argument on purpose. The values to persist are the ones the
 * mailbox already holds, and tile[0] must not read that mailbox itself (that
 * mistake journalled zeros while the sequence number climbed), so the setter's
 * tile[1] side passes the pair down as an argument. A save of "whatever the
 * host last set" is the only contract that cannot half-persist.
 */
static SCPI_FPTR_CMD scpi_result_t persist_save(scpi_t *context)
{
    if (!csd_diag_trim_save(csd_mb_get_trim())) {
        /* Distinguished from a rejected parameter on purpose: the value stands
         * for this power cycle, only its persistence failed. SYSTem:ERRor:NEXT?
         * is how the rig tells "saved" from "set but not journalled" -- silence
         * here would let a dead QSPI partition look like a working save. */
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
        return SCPI_RES_ERR;
    }
    return SCPI_RES_OK;
}

/* ---- POWER:TIMEout -- owner case 14, directive 2026-11-05 -----------------
 * Semantics are the owner's, verbatim: 0 enters power saving immediately and
 * stays there; a negative value keeps the box powered up indefinitely; any
 * other positive value is the timeout in seconds; the default is 300 s. Every
 * value OTHER than 0 is journalled and re-applied at power up -- 0 is transient
 * by spec, so a reboot must never leave the box asleep.
 *
 * The setter only ASKS. A sequence runs to completion inside csd_power_poll and
 * exactly one task owns that (csd_power.h); running a teardown from this task
 * would put two executors on one codec. A set of 0 therefore takes effect on the
 * next 0.5 s tick, which is immediate to a human and keeps the invariant true.
 */
static SCPI_FPTR_CMD scpi_result_t power_timeout_set(scpi_t *context)
{
    char     txt[24];
    int32_t  sec;

    if (!param_text(context, txt, sizeof txt, TRUE)) goto bad;
    if (!parse_snum(txt, strlen(txt), &sec))         goto bad;
    if (sec < -1)    sec = -1;            /* any negative means "never"       */
    if (sec > 86400) goto bad;            /* 24 h ceiling; above is a typo    */

    csd_power_set_timeout_sec((int)sec);

    /* Persist anything other than 0. The record carries the trim pair too, so
     * the current pair is passed through -- tile[0] must not read this tile's
     * mailbox (that exact mistake journalled zeros while seq climbed). A failed
     * journal is reported in the error queue and does NOT undo the live value:
     * "set but not saved" must never look like "saved", and must never look
     * like "rejected" either. */
    if (sec != 0) {
        if (!csd_diag_pw_timeout_save((int)sec, csd_mb_get_trim())) {
            SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
        }
    }
    return SCPI_RES_OK;
bad:
    SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
    return SCPI_RES_ERR;
}

static SCPI_FPTR_CMD scpi_result_t power_timeout_q(scpi_t *context)
{
    /* Read the RUNTIME value, not the journalled one: what the box is doing now
     * is the question a rig can act on. They differ only after a set of 0, which
     * is deliberately not persisted. */
    SCPI_ResultInt32(context, (int32_t) csd_power_get_timeout_sec());
    return SCPI_RES_OK;
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

/* Defined below; the two new commands reply through them rather than duplicating
 * the adopt-and-reply logic. */
static SCPI_FPTR_CMD scpi_result_t vol_trim_status_q(scpi_t *context);
static SCPI_FPTR_CMD scpi_result_t vol_trim_load(scpi_t *context);

/* VOLume:TRIM:SAVe -> journal the currently applied pair, then reply the status
 * words. A rig uses this to prove the write path without first changing a value.
 *
 * The pair is read from the mailbox HERE, on tile[1], and sent as an argument:
 * the store runs on tile[0], and reading the mailbox there would journal
 * tile[0]'s never-written copy -- the bug that once made seq climb while the
 * stored value stayed 0 dB. */
static SCPI_FPTR_CMD scpi_result_t vol_trim_save(scpi_t *context)
{
    unsigned packed = csd_mb_get_trim();
    if (!csd_diag_trim_save(packed)) {
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
        return SCPI_RES_ERR;
    }
    return vol_trim_status_q(context);
}

/* VOLume:TRIM:RESET -> write the default condition (0 dB, 300 s) at the CURRENT
 * record version, then adopt it through the normal load path. Recovery for a
 * bisect that left a record this image cannot use: a newer record is refused
 * rather than misread, and this reset supersedes it by sequence number. */
static SCPI_FPTR_CMD scpi_result_t vol_trim_reset(scpi_t *context)
{
    if (csd_diag_trim_reset_store() == 3) {       /* 3 = store reported an error */
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
        return SCPI_RES_ERR;
    }
    return vol_trim_load(context);                /* adopt what the reset wrote */
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
    char buf[512];
    unsigned len = 0;
    /* 11 words: the original seven plus the four record-version counters added
     * with the versioned store (7 checksum rejects, 8 unusable version,
     * 9 migrated, 10 reset to defaults). They are APPENDED, never inserted, so a
     * rig that reads seven still gets the same seven in the same order. */
    for (unsigned i = 0; i < 11u; i++) {
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
    unsigned packed = 0u;
    if (!csd_diag_trim_load(&packed)) {
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
        return SCPI_RES_ERR;
    }
    /* Applied HERE, on tile[1] -- the tile whose local static csd_mb_trim
     * actually is. The store on tile[0] cannot publish this; see
     * csd_diag_regs.xc. Same writer as the SCPI set path above. The
     * registers are re-written too (directive 2026-11-04): LOAD's contract
     * is "adopt what is in flash", and adoption now means the chip, not
     * only the mailbox. */
    csd_mb_put_trim(packed);
    csd_diag_vol_reapply();   /* adoption must reach the chip, not just the mailbox */
    {
        unsigned q31 = csd_trim_master_trim_q31(packed);
        for (unsigned ti = 0; ti < 4u; ti++) {
            unsigned tb = csd_trim_reg_byte(q31, ti);
            if (!csd_diag_reg_write(CSD_TRIM_REG_BASE + ti, tb)) {
                SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
                break;
            }
        }
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
#endif /* !SPECIALTYCIRCUITS_RELEASE (part 1: MIXer/DIAG:I2S probes) */

/* DEBUG:I2S? deliberately does NOT escape the release gate: its producer
 * csd_mb_note_i2s_delta is an empty stub in release builds, so the counter
 * would read a clean 0% bad. A number that lies is worse than no number. */

#ifndef SPECIALTYCIRCUITS_RELEASE   /* reopened: the remainder of the debug
                                     * block stays release-gated exactly as
                                     * before, so a release build carries no
                                     * dead instrumentation and no row can
                                     * reference a callback that is not there */

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

/* ---- DIAG:UPSAMPLing -- XMOS 8x upsampling + ES9219Q internal FIR bypass --
 *
 * ONE control, not two. Enabling the chip's OSF bypass without feeding it 8x
 * data starves the modulators, and feeding it 8x data with its FIR still
 * engaged interpolates twice; both are silence or noise rather than a subtle
 * artefact, so the pair is settable only together (es9219q.h, datasheet v1.3
 * Table 3 p13 and p22).
 *
 * DEFAULT OFF: the built-in filter is the acoustically proven path.
 *
 * The reply is always the resulting state, not the request: "applied" is 0
 * when the codec is not actually being fed a 384 kHz or 352.8 kHz frame clock,
 * which is the case until the I2S output clock is decoupled from the USB
 * stream rate. A silent refusal would be the worst possible failure here.
 *
 * Rows live outside the release gate on purpose: this is a product control. */
static void ups_reply(scpi_t *context)
{
    unsigned ap = 0u, sf = 0u, rf = 0u;
    csd_diag_ups_get(&ap, &sf, &rf);
    char buf[512];
    int n = snprintf(buf, sizeof(buf), "%u,%u,%u,%u,%u",
                     csd_ups_is_enabled(), ap, sf, rf, csd_ups_diag_refusals());
    if (n > 0) SCPI_ResultCharacters(context, buf, n);
}

static SCPI_FPTR_CMD scpi_result_t ups_q(scpi_t *context)
{
    ups_reply(context);
    return SCPI_RES_OK;
}

/* 2026-11-09, ratio 2 bring-up: read-only truth table for the upsampler.
 * WHY: DIAG:UPSAMPLing:STATe? reports the CODEC's OSF grant as field 2, so it
 * cannot distinguish 'interpolator armed and pacing' from 'never armed, pacer
 * pinned to 1'. That blind spot cost a full day: the wire emitted one word per
 * host frame at every setting of that flag while it read 1,1,384000,0,0.
 * CHANGE: new row, no data path touched. Reply order:
 *   armed,enabled,want,in_hz,out_hz,ratio,frames,popsL,popsR,repsL,repsR,occHL,occHR
 * A healthy ratio-2 stream must show armed=1, ratio=2, in=192000, out=384000,
 * and pops ~= ratio * frames per channel. pops stuck at 0 with reps climbing
 * means the ring is starved -- the audiohub is not pacing.
 * REVERT: delete this callback and its table row. */
extern unsigned xua_csd_get_usb_ratio(void);   /* lib_xua, xua_audiohub.xc */
/* 2026-11-09 mic decimation phase: the phase bound lives in the audiohub
 * (file-scope, owner compute constraint 2026-11-09 -- the hot loop must own
 * its compare operand); this task only sets and reads it, same pattern as
 * xua_csd_set_usb_ratio. REVERT: delete these two externs with the PHASE
 * callbacks and rows. */
extern void     xua_csd_set_mic_phase(unsigned p);
extern unsigned xua_csd_get_mic_phase(void);
/* lib_xua, xua_audiohub.xc: port decimator observation (see the decimator
 * there -- declared locally like the ratio/mic-phase externs above). */
extern unsigned xua_csd_get_decim_slot(void);
extern unsigned xua_csd_get_decim_pub(void);
/* AUTO sentinel shared with the audiohub (xua_audiohub.xc
 * CSD_MIC_PHASE_AUTO_X must equal this; the decimator lives there). */
#define CSD_MIC_PHASE_AUTO 0xFFFFFFFFu
/* lib_xua, xua_ep0_uacreqs.xc: what the USB host asked the single shared clock
 * for, in order. See the recorder there for the reason. ok: 1 accepted,
 * 0 rejected by the family check, 2 seen but equal to the current value. */
extern unsigned xua_csd_clk_reqs(void);
extern unsigned xua_csd_clk_accepted(void);
extern unsigned xua_csd_clk_rejected(void);
extern unsigned xua_csd_clk_hist_hz(unsigned i);
extern unsigned xua_csd_clk_hist_ok(unsigned i);

/* 2026-11-09, ratio 2 bring-up: read-only. Separates 'the host never asked for
 * 192 kHz' from 'the host asked and the mic's 384 kHz request overwrote it'.
 * Reply: reqs,accepted,rejected, then 8 x (hz:ok), oldest slot first is index 0
 * of a wrapping ring. REVERT: delete this callback and its table row. */
static SCPI_FPTR_CMD scpi_result_t ups_clk_q(scpi_t *context)
{
    char buf[512];
    int n = snprintf(buf, sizeof(buf), "%u,%u,%u",
                     xua_csd_clk_reqs(), xua_csd_clk_accepted(), xua_csd_clk_rejected());
    for (unsigned i = 0u; i < 8u && n > 0 && n < (int)sizeof(buf) - 12; i++) {
        n += snprintf(buf + n, sizeof(buf) - n, ",%u:%u",
                      xua_csd_clk_hist_hz(i), xua_csd_clk_hist_ok(i));
    }
    if (n > 0) SCPI_ResultCharacters(context, buf, n);
    return SCPI_RES_OK;
}
/* 2026-11-09 DIAG:UPSAMPLing:DIVider? (assistant). Reports the EXACT triple the
 * audiohub used to program the I2S ports -- curSamFreq, mClk, divide, numBits,
 * dsdMode -- next to what this board tried to force (forced_samFreq,
 * forced_mClk, seq). WHY: the owner's config gate is 'ES9219Q at 384k, ADC1 at
 * 384k, USB out 48k, USB in 48k'. divide = mClk/(curSamFreq*numBits) at
 * xua_audiohub.xc:1192 is the only thing that sets the wire frequency, and it
 * reads the host's SET_CUR value, not the board's forced one, so this is the
 * measurement that decides whether that config is achievable as written.
 * Expected BCLK = mClk/divide; expected LRCLK = BCLK/numBits.
 * REVERT: delete this callback and its table entry. */
extern unsigned g_csd_div_curSamFreq, g_csd_div_mClk, g_csd_div_divide;
extern unsigned g_csd_div_numBits, g_csd_div_dsdMode;
extern unsigned g_csd_forced_samFreq, g_csd_forced_mClk, g_csd_forced_seq;
static SCPI_FPTR_CMD scpi_result_t ups_div_q(scpi_t *context)
{
    char buf[128];
    int n = snprintf(buf, sizeof(buf), "%u,%u,%u,%u,%u,%u,%u,%u",
                     g_csd_div_curSamFreq, g_csd_div_mClk, g_csd_div_divide,
                     g_csd_div_numBits, g_csd_div_dsdMode,
                     g_csd_forced_samFreq, g_csd_forced_mClk, g_csd_forced_seq);
    if (n > 0) SCPI_ResultCharacters(context, buf, n);
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t ups_info_q(scpi_t *context)
{
    /* 2026-11-09: widened 192 -> 352. WHY: the mic-phase tail below appends
     * 12 more fields; ten u32 digits + comma per field makes the old size
     * reachable, and snprintf returns the WOULD-BE length, which the reply
     * call below then used -- an out-of-bounds read of `buf` at truncation,
     * latent before because the 18-field worst case (~200 B) was rare. The
     * clamp turns any future truncation into a short-but-in-bounds reply;
     * SCPI_Write's own REPLY_MAX cap and reply_truncs counter stay the host
     * visible truncation report. REVERT for both: restore char buf[192] and
     * drop the clamp line. */
    /* 2026-11-10: 352 -> 640 to match REPLY_MAX; snprintf returns the
     * WOULD-BE length, so an undersized buffer here truncates INFO? at
     * ~43 fields. REVERT: 352. */
    char buf[2400];   /* dp172: was 1216 — truncated the tail at 114 fields */
    extern unsigned g_csd_c_tail, g_csd_f_wt, g_csd_f_wait_n,
                    g_csd_f_skip_n, g_csd_f_armed_n; /* REVERT(dp168) */
    int n = snprintf(buf, sizeof(buf),
                     "%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u",
                     csd_ups_armed(), csd_ups_is_enabled(), csd_ups_get_want(),
                     csd_ups_diag_in_hz(), csd_ups_diag_out_hz(),
                     xua_csd_get_usb_ratio(), csd_ups_diag_frames(),
                     csd_ups_diag_pops(0u), csd_ups_diag_pops(1u),
                     csd_ups_diag_repeats(0u), csd_ups_diag_repeats(1u),
                     csd_ups_diag_occ_high(0u), csd_ups_diag_occ_high(1u),
                     csd_ups_diag_rate_rejects(),
                     /* Block-engine witnesses replacing the 2026-11-09 tile
                      * probe (same four slots, same positions: append-only
                      * alignment rule): computes run, worst compute burst in
                      * 10 ns ticks, rate hand-overs seen, last rate asked. */
                     csd_ups_diag_blocks(), csd_ups_diag_block_ticks_max(),
                     csd_ups_diag_rate_calls(), csd_ups_diag_last_req_hz());
    /* 2026-11-09 mic decimation phase (owner request). WHY: the rig must be
     * able to prove the phase took effect rather than trust the setter, and
     * INFO? is the one reply the rig already parses positionally.
     * CHANGE: 12 fields APPENDED at the end, original 18 untouched in place
     * and order (append-only rule): phase,ratio,mode,cycles,fallbacks,
     * hits0..hits7. phase = EFFECTIVE slot (AUTO reported as ratio-1, the
     * slot it actually latches); mode 0=auto,1=operator-set; cycles = mic
     * capture transfers; hits[s] = transfers whose word came from slot s;
     * fallbacks = transfers where the selected phase never arrived. Counter
     * scale: cycles is per transfer event, hits and fallbacks are PER CHANNEL
     * (two increments per cycle) -- L and R latch independently, and a
     * one-sided miss (e.g. only one fill site calling note) must be visible.
     * Proof for phase p: delta(hits[p]) == 2 * delta(cycles) with fallbacks
     * flat. REVERT: delete this block and widen the format string back. */
    /* 2026-11-09: 6 more trailing fields (phase-origin alignment record).
     * Append-only, so every field index that existing parsers rely on is
     * unchanged. REVERT: drop the six fields and restore '13 * 11'. */
    /* Raw ADC1 witness fields appended 2026-11-09 (cnt0,cnt1,or0,or1,sum0,
     * sum1): what the IN instructions on the ADC ports actually returned.
     * Needed because the buffer-level evidence cannot distinguish "the port
     * read nothing" from "something overwrote it later". Append-only again,
     * so all existing field indices hold. REVERT: drop 6 specifiers/args and
     * restore '19 * 11'. */
    extern unsigned g_csd_adc_cnt[2];
    extern unsigned g_csd_adc_or[2];
    extern unsigned g_csd_adc_sum[2];
    extern unsigned g_csd_adc_ctr_seen;
    extern unsigned g_csd_adc_fc_seen;
    extern unsigned g_csd_gate_true, g_csd_gate_false, g_csd_ratio_live;
    /* dp160 2026-11-13 pacing witnesses (see xua_audiohub.xc decl block).
     * REVERT with the 7 tail fields. */
    extern unsigned g_csd_wire_ticks_val, g_csd_wire_rem_val;
    extern unsigned g_csd_span_min, g_csd_span_max, g_csd_resync_n; /* REVERT(dp171) */
    extern unsigned g_csd_psw_alive, g_csd_psw_armed, g_csd_psw_latched,
                    g_csd_psw_lead, g_csd_psw_tmr, g_csd_psw_prod; /* REVERT(dp172) */
    extern unsigned g_csd_floor_n, g_csd_f_dmax;                   /* REVERT(dp172) */
    /* REVERT(dp181): mic zero-birth witnesses (xua_audiohub.xc). */
    extern unsigned g_csd_w_portn[2], g_csd_w_portz[2];
    extern unsigned g_csd_w_pubn[2],  g_csd_w_pubz[2];
    extern unsigned g_csd_w_shipn[2], g_csd_w_shipz[2];
    extern unsigned g_csd_w_exch;
    /* REVERT(s15-diag): receive_command wake-code histogram (xua_audiohub.xc). */
    extern unsigned g_csd_cmd_tot, g_csd_cmd_sr, g_csd_cmd_start;
    extern unsigned g_csd_cmd_stop, g_csd_cmd_unh, g_csd_cmd_last;
    unsigned csd_ups_diag_rc_total(void);
    unsigned csd_ups_diag_rc_site(unsigned i);
    unsigned csd_ups_diag_s16(unsigned i); /* REVERT(s16-diag) */
    extern unsigned g_csd_w_iter;  /* REVERT(dp203) */
    extern unsigned g_csd_w_dsdn;  /* REVERT(dp203) */
    extern unsigned g_csd_w_portu[2]; /* REVERT(dp203) */
    extern unsigned g_csd_handoff_calls, g_csd_task_loops,
                    g_csd_task_past_take;                    /* REVERT(dp172) */
    extern unsigned g_csd_p_slip_max, g_csd_p_pnext, g_csd_p_now,
                    g_csd_p_force, g_csd_p_ptk;              /* REVERT(dp174) */
    extern unsigned g_csd_f_add_n, g_csd_dn_min, g_csd_dn_max; /* REVERT(dp174) */
    extern unsigned g_csd_claim_pace_n;                        /* REVERT(dp177) */
    extern unsigned g_csd_it_adc_ticks, g_csd_it_pre_ticks, g_csd_fl_ticks;
    extern unsigned g_csd_it_n, g_csd_fl_wait;
    extern unsigned g_csd_tt_ticks, g_csd_tt_n;
    extern unsigned g_csd_xfer_tok;
    extern unsigned g_csd_xfer_else;
    extern unsigned g_csd_xfer_n, g_csd_xfer_ticks, g_csd_xfer_max;
    extern unsigned g_csd_xt_ct, g_csd_xt_in, g_csd_xt_out;
    extern unsigned g_csd_xt_ct_n, g_csd_xt_in_n, g_csd_xt_out_n;
    extern unsigned g_csd_xt_ubm, g_csd_xt_ubm_n, g_csd_xt_ubm_max;
    extern unsigned g_csd_ubm_calls, g_csd_eq_frames, g_csd_eq_delay_frames,
                    g_csd_eq_ticks_max;
    extern unsigned g_csd_bq_calls[2], g_csd_bq_in_or[2], g_csd_bq_in_last[2];
    extern unsigned g_csd_bq_out_last[2], g_csd_bq_in_nz[2];
    extern unsigned g_csd_bq_out_or[2];
    extern unsigned g_csd_bq_cfg_calls, g_csd_bq_clear_calls;
    extern unsigned g_csd_lrclk_emit, g_csd_lrclk_ts_min, g_csd_lrclk_ts_max;
    extern unsigned g_csd_ubm_reg_tot[5], g_csd_ubm_reg_max[5];
    /* Relocated EQ task witnesses (2026-11-10, csd_dsp.xc): underrun = pop
     * skipped (pipeline fill on enable, or dead task -- must be transient/0),
     * overrun = push skipped (EQ task cannot sustain frame rate -- hard
     * qualification failure, must read 0), occ_high = packed ring
     * high-water (in << 16 | out), cfg_adopt = engine config adoptions run
     * by the task. REVERT with the ring. */
    extern unsigned g_csd_eq_underrun, g_csd_eq_overrun, g_csd_eq_occ_high;
    extern unsigned g_csd_eq_cfg_adopt;
    if (n > 0) {   /* REVERT(dp182): gate removed — was n < sizeof-8-82*11 */ /* tail is 80 fields
                                                          * after eq_kernel_src
                                                          * (2026-11-11 tap) */
        /* REVERT(armed-capture bench 2026-11-17): named witnesses. */
        extern unsigned g_csd_flip_n, g_csd_tk_n;
        extern unsigned g_csd_slot_n, g_csd_pub2_n;
        extern volatile unsigned s_rec_on;   /* csd_ups_block.c recorder */
        extern unsigned csd_probe_pop_count(void);
        extern unsigned s_feedcalls_task, s_feedcalls_audio;
        extern unsigned csd_probe_en_get(void);
        extern unsigned g_csd_fb1; extern unsigned g_csd_fb2;
        extern unsigned csd_ups_dbg_pushn(void); extern unsigned csd_ups_dbg_fidx(void);
        extern unsigned g_csd_ft1, g_csd_ft2, g_csd_ft3;
        extern unsigned g_csd_ft4, g_csd_ft5, g_csd_ep0, g_csd_ep1, g_csd_ewz;
        extern unsigned g_csd_bp;
        const unsigned s_rec_on_w = s_rec_on;
        const unsigned s_probe_pop_n_w = csd_probe_pop_count();
        const unsigned ratio = xua_csd_get_usb_ratio();
        unsigned ph = xua_csd_get_mic_phase();
        const unsigned mode = (ph == CSD_MIC_PHASE_AUTO) ? 0u : 1u;
        if (ph == CSD_MIC_PHASE_AUTO) ph = (ratio > 0u) ? (ratio - 1u) : 0u;
        n += snprintf(buf + n, sizeof(buf) - (unsigned)n,
                      /* 2026-11-09 fix (found by sa-0-5865978d): this format
                       * string lost its LEADING comma when the alignment fields
                       * were appended, so the last base field and `phase` merged
                       * ("48000"+"7" -> "480007") and the reply parsed as 36
                       * fields instead of 37, silently shifting every positional
                       * reader. CHANGE: leading comma restored, 19 specifiers for
                       * 19 arguments (phase,ratio,mode,cycles,fallbacks,
                       * hits0..7,align_slot,qrd0,qrd1,occ0,occ1,cycles).
                       * REVERT: remove the leading comma (do not -- it is a bug). */
                      /* 2026-11-10: specifier count and argument count are the
                       * contract; check both (a truncated silent field drop is
                       * how the 28/28 bug hid). The tail is 37 fields after the
                       * 2026-11-10 removal of the six deleted-engine timer
                       * probes from the end of the reply. */
                      ",%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u"
                      ",%u,%u,%u,%u,%u,%u,%u,%u,%u"
                      ",%u,%u,%u,%u,%u,%u,%u,%u,%u"
                      ",%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u"
                      /* 77 tail fields vs 73 specifiers as shipped (found
                       * 2026-11-10 on image .96): snprintf drops surplus
                       * ARGS in silence, so the four witnesses appended at
                       * the end (fields 91..94) never reached the wire --
                       * exactly the silent-field-drop this file's 11-09
                       * note warns to check for. Specifier count ==
                       * argument count is the contract. REVERT with 91..94. */
                      ",%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u"
                      /* 3 more (2026-11-11, wire-word capture tap): indices
                       * 95..97 tap_fires (committed L/R pairs at the selected
                       * phase), tap_last_phase (ring phase of last commit),
                       * tap_slot_eff (resolved presentation slot). Passing
                       * armed runs require delta(tap_fires) > 0 and
                       * tap_last_phase == tap_slot_eff. REVERT with the tap. */
                      /* 2026-11-12: 11 -> 12 specifiers. regen_info_map.py
                       * proved 107 args vs 106 %u: snprintf dropped the LAST
                       * arg (g_csd_eq_stalls) in silence — the exact failure
                       * the 11-10 note above warns about. REVERT: 11. */
                      ",%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u"
                      /* dp160 2026-11-13: 7 specifiers for the 7 pacing
                       * witnesses appended below (specifier==arg contract).
                       * REVERT with those fields. */
                      ",%u,%u,%u,%u,%u,%u,%u"
                      ",%u,%u,%u,%u,%u,%u,%u,%u"
                      ",%u,%u,%u"
                      ",%u,%u,%u,%u,%u,%u,%u,%u"
                      /* dp182 FIX: the comma that used to follow the dp177
                       * literal terminated the FORMAT string there — snprintf
                       * took 116 specifiers, silently dropped every later
                       * argument INCLUDING the whole dp181 format literal
                       * (which became argument #117). Specifier-after-comma
                       * is the contract killer. Format now runs unbroken to
                       * the dp181 chunk. REVERT: restore '",%u",'. */
                      ",%u"   /* REVERT(dp177): claim pacer */
                      /* 13 more (REVERT(dp181)): mic zero-birth witnesses
                       * (xua_audiohub.xc g_csd_w_*): portn0/1, portz0/1,
                       * pubn0/1, pubz0/1, shipn0/1, shipz0/1, exch.
                       * REVERT(dp203): +1 = iter, dsdn, portu0/1 (skip hunt).
                       * REVERT(s14): +2 = push_n, push_over (double-buffer
                       * commit parity + capacity-backoff witness). */
                      ",%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u"
                      ",%u,%u"
                      /* REVERT(s15-diag): +11 = cmd_tot, cmd_sr, cmd_start,
                       * cmd_stop, cmd_unh, cmd_last (receive_command wake
                       * codes, xua_audiohub.xc) + rc_total, rc_site0..3
                       * (ups_reconfigure attribution, csd_ups_block.c).
                       * REVERT(s16-diag): trailing comma inside the s15
                       * literal removed (it was the last literal, so it
                       * only emitted a stray separator; as a NON-last
                       * literal it would silently turn every later literal
                       * into an extra arg — the dp181 comma trap). */
                      ",%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u"
                      /* REVERT(s16-diag): +8 = blkzL, blkzR (last-4
                       * pop-block zero histograms, 8 b/slot, newest low),
                       * popzL, popzR (cumulative zero pops), pbase (last
                       * commit base), pocc0/1 (occ after last commit),
                       * ovlv (stale-region overwrite count).
                       * +4 = lgg0/1 (lead-gate holds), lggz0/1 (lead-gate
                       * holds of a zero last word). */
                      ",%u,%u,%u,%u,%u,%u,%u,%u"
                      ",%u,%u,%u,%u"
                      /* REVERT(bp21): +3 = mirror-selection witnesses
                       * (csd_dsp.xc): pub_n (pop-side pairs published, one
                       * per host frame at slot phase 0), copy_n (mirror
                       * copies, one per audio frame), hold_n (copies with
                       * no new pair). delta(pub_n)==delta(copy_n) proves
                       * skew-free 1:1 mirror-to-pop accounting. */
                      ",%u,%u,%u"
                      /* REVERT(bp31): +2 = mic-frame-aligned mirror skew
                       * witnesses (csd_dsp.xc): mir_relock_n (FIFO window
                       * escapes) and mir_lag (prod_n - cursor at the last
                       * read: bounded constant == skew-free 1:1). */
                      /* REVERT(armed-capture bench 2026-11-17): +4 =
                       * flip_n (audiohub host-frame flips), tk_n
                       * (csd_mic_tap_take entries), slot_n (csd_ups_slot
                       * pop entries), pub2_n (mod-8 FIFO publishes),
                       * recon (recorder on), popn (pop ring count),
                       * fct (feed calls task), fca (feed calls audio),
                       * pen (probe enable stat: 1 on, 2 OOM),
                       * d2n, d2rows, d2nullq, d2nullr (dump witnesses),
                       * ft1/ft2/ft3 (feed past-gates / probe-on / !rec_on),
                       * ft4/ft5 (zero/nonzero feed pair), ep0/ep1 (audio-path
                       * push taken/skipped), ewz (audio word nonzero);
                       * bp (block pushes) REUSES the ft4 slot. */
                      ",%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u",

                      ph, ratio, mode,
                      /* 2026-11-10: these 16 fields reported the deleted latch
                       * decimator. Same field COUNT, new meaning, so every
                       * positional reader stays aligned (the field-count bug in
                       * the note above is why count is sacred). F1 = the count
                       * the port decimator presents, F2 = words it has presented
                       * to the mic stream (expect ~= host_rate x seconds); F3..F16
                       * reserved at 0 for the phase sweep. */
                      xua_csd_get_decim_slot(), xua_csd_get_decim_pub(),
                      /* 2026-11-12 gate attribution in the first 3 reserved
                       * zeros: transfer-gate true/false branch counts and the
                       * live g_csd_usb_ratio at last gate-true. REVERT with
                       * CSD_ADC_DIAG. */
                      g_csd_gate_true, g_csd_gate_false, g_csd_ratio_live,
                      /* transfer-frame cost witnesses (2026-11-12): total
                       * 10 ns ticks and count over gate-entry..pre-floor.
                       * REVERT with CSD_ADC_DIAG. */
                      g_csd_tt_ticks, g_csd_tt_n,
                      0u, 0u, 0u, 0u, 0u, 0u, 0u, 0u,
                      /* 2026-11-11 armed-frame state-machine witnesses (last 4
                       * reserved zeros of this 16-field block; count sacred):
                       * ctr_seen = bitmask of audioToUsbRatioCounter values the
                       * decimator gates ever saw; fc_seen = bitmask of
                       * frameCount at the ADC reads; xfer_tok = collect
                       * (prefetch) transfers; xfer_else = plain transfers.
                       * REVERT with CSD_ADC_DIAG. */
                      g_csd_adc_ctr_seen, g_csd_adc_fc_seen,
                      g_csd_xfer_tok, g_csd_xfer_else,
                      /* raw ADC1 witness (see xua_audiohub.xc): all zero with a
                       * nonzero stimulus means the IN instructions never saw the
                       * wire; nonzero means the loss is downstream of the read. */
                      g_csd_adc_cnt[0], g_csd_adc_cnt[1],
                      g_csd_adc_or[0], g_csd_adc_or[1],
                      g_csd_adc_sum[0], g_csd_adc_sum[1],
                      /* 3 more (2026-11-10): transfer count, total stall ticks,
                       * worst stall tick -- 10 ns units. UNCONDITIONAL on
                       * purpose: this file is C and does NOT receive
                       * CSD_EXTRA_FLAGS (measured: with the fields behind
                       * #ifdef CSD_XFER_TIMING the reply stayed at 43 fields
                       * although the flag was present for the XC sources), so a
                       * gated readout is an invisible no-op. The globals are
                       * defined unconditionally in xua_audiohub.xc; only the
                       * increments are gated, so a build without the flag reads
                       * three zeros. */
                      g_csd_xfer_n, g_csd_xfer_ticks, g_csd_xfer_max,
                      g_csd_xt_ct, g_csd_xt_in, g_csd_xt_out,
                      g_csd_xt_ct_n, g_csd_xt_in_n, g_csd_xt_out_n,
                      g_csd_xt_ubm, g_csd_xt_ubm_n, g_csd_xt_ubm_max,
                      /* 16 more (2026-11-10): EQ-path witnesses for the
                       * 384 kHz left-channel shelf fault. Names (append-only,
                       * indices 55..70): ubm_calls, eq_frames, eq_delay_frames,
                       * eq_ticks_max (10 ns ticks, worst burst around the two
                       * adsp_biquad calls), bq_calls_l, bq_calls_r, bq_in_or_l,
                       * bq_in_or_r, bq_in_last_l, bq_in_last_r, bq_out_last_l,
                       * bq_out_last_r, bq_in_nz_l, bq_in_nz_r, bq_cfg_calls,
                       * bq_clear_calls. Increments are gated by
                       * CSD_EQ_DIAG_WITNESS (build flag); the globals exist
                       * always so this file links without the flag. A dead
                       * channel shows calls advancing with in_or stuck or
                       * in_nz==0 while the other channel lives.
                       * REVERT: delete these 16 fields. */
                      g_csd_ubm_calls, g_csd_eq_frames, g_csd_eq_delay_frames,
                      g_csd_eq_ticks_max,
                      g_csd_bq_calls[0], g_csd_bq_calls[1],
                      g_csd_bq_in_or[0], g_csd_bq_in_or[1],
                      g_csd_bq_in_last[0], g_csd_bq_in_last[1],
                      g_csd_bq_out_last[0], g_csd_bq_out_last[1],
                      g_csd_bq_in_nz[0], g_csd_bq_in_nz[1],
                      g_csd_bq_cfg_calls, g_csd_bq_clear_calls,
                      g_csd_bq_out_or[0], g_csd_bq_out_or[1],
                      g_csd_lrclk_emit, g_csd_lrclk_ts_min,
                      g_csd_lrclk_ts_max,
                      /* 10 more (2026-11-10): UBM sub-region cost witnesses
                       * (indices 76..85): ap_tot,ap_max,pw_tot,pw_max,
                       * gen_tot,gen_max,dsp_tot,dsp_max,ubm_tot,ubm_max --
                       * reference (10 ns) ticks; divide tot by xt_ubm_n (or
                       * ubm_calls) for the mean. REVERT: delete. */
                      g_csd_ubm_reg_tot[0], g_csd_ubm_reg_max[0],
                      g_csd_ubm_reg_tot[1], g_csd_ubm_reg_max[1],
                      g_csd_ubm_reg_tot[2], g_csd_ubm_reg_max[2],
                      g_csd_ubm_reg_tot[3], g_csd_ubm_reg_max[3],
                      g_csd_ubm_reg_tot[4], g_csd_ubm_reg_max[4],
                      /* 4 more (2026-11-10): relocated EQ task witnesses
                       * (indices 86..89): eq_underrun, eq_overrun,
                       * eq_occ_high (in<<16|out), eq_cfg_adopt. Passing runs
                       * require eq_overrun == 0 and eq_underrun transient
                       * (pipeline fill only). REVERT: delete these 4 fields. */
                      g_csd_eq_underrun, g_csd_eq_overrun,
                      g_csd_eq_occ_high, g_csd_eq_cfg_adopt,
                      /* 1 more (2026-11-10, host coefficient aperture):
                       * index 90 eq_kernel_src -- the TASK-APPLIED truth of
                       * the single biquad section: 0 = shipped default
                       * kernel (unity/identity), 1 = host-composed override
                       * (DIAG:DSP:COEFFicient). Written by the EQ task at
                       * config, so it reports what the engine is actually
                       * running, unlike the request-side readback. A
                       * measurement is attributable only through this
                       * field. REVERT: delete this field. */
                      csd_dsp_bq_diag_kernel_src(),
                      /* 4 more (2026-11-10, atomic kernel block write +
                       * soft-ramp): indices 91..94 eq_pause_events,
                       * eq_pause_ticks_max (reference ticks of the worst
                       * engage(0)..engage() window on the EQ task),
                       * krnl_ramp_events (codec-server ramp halves run,
                       * mirrored from the tile[0] owner's reply),
                       * krnl_adopt_timeouts (adoption waits that hit the
                       * bound -- passing runs read 0). REVERT: delete. */
                      g_csd_eq_pause_events, g_csd_eq_pause_ticks_max,
                      g_csd_krnl_ramp_events, g_csd_krnl_adopt_timeouts,
                      csd_ups_diag_tap_fires(), csd_ups_diag_tap_phase(),
                      csd_ups_diag_tap_slot_eff(),
                      /* 2026-11-11 armed-mic feed witnesses (fields 98, 99):
                       * 98 = feed stores done by the UBM feed block (must
                       * grow ~ubm_calls while armed; 0 while disarmed),
                       * 99 = csd_feed_owner as the audio core sees it.
                       * REVERT with the feed. */
                      g_csd_feed_writes, csd_feed_owner,
                      /* 3 more (2026-11-11, EXPERIMENT 11.11.30 stuck-compute
                       * witnesses, REVERT with the tap): feed_enter counts
                       * block-engine entries, feed_pc the (phase<<1|chan) of
                       * the last compute entered, feed_in_pc 1 while one is
                       * running. A frozen EQ task with feed_in_pc=1 names the
                       * stuck fd_block_fir_compute. */
                      csd_ups_diag_feed_enter(), csd_ups_diag_feed_pc(),
                      csd_ups_diag_feed_in_pc(),
                      g_csd_eq_stalls,   /* 11.11.31 armed flow-control timeouts */
                      /* 7 more (dp160 2026-11-13, armed self-pacing probe):
                       * wire_ticks_val/wire_rem_val = floor stride latched at
                       * loop entry (0 => floor never armed), it_adc = top..
                       * end-of-I/O ticks/iter, it_pre = I/O..pre-floor ticks,
                       * fl_ticks = floor-wait ticks, it_n = iterations,
                       * fl_wait = iterations whose floor wait >= 20 ticks.
                       * REVERT: delete these 7 fields. */
g_csd_span_min, g_csd_span_max,
                      /* REVERT(dp168): tail = floor diagnostics:
                       * it_adc=g_csd_f_wt (floor stride ticks latched),
                       * it_pre=g_csd_f_wait_n (blocking waits taken/s),
                       * fl_ticks=g_csd_f_skip_n (expired-deadline skips/s),
                       * it_n=g_csd_f_armed_n (floor latches, event),
                       * fl_wait=g_csd_c_tail (frame rate/s) */
                      g_csd_f_wt, g_csd_f_wait_n,
                      g_csd_f_skip_n, g_csd_resync_n, g_csd_c_tail,
                      /* REVERT(dp172): producer liveness ladder + floor
                       * entry/debt. REVERT: delete these 8 fields. */
                      g_csd_psw_alive, g_csd_psw_armed, g_csd_psw_latched,
                      g_csd_psw_lead, g_csd_psw_tmr, g_csd_psw_prod,
                      g_csd_floor_n, g_csd_f_dmax,
                      g_csd_handoff_calls, g_csd_task_loops,
                      g_csd_task_past_take,
                      g_csd_p_slip_max, g_csd_p_pnext, g_csd_p_now,
                      g_csd_p_force, g_csd_p_ptk, g_csd_f_add_n,
                      g_csd_dn_min, g_csd_dn_max,
                      g_csd_claim_pace_n,
                      /* REVERT(dp181): mic zero-birth witnesses. */
                      g_csd_w_portn[0], g_csd_w_portn[1],
                      g_csd_w_portz[0], g_csd_w_portz[1],
                      g_csd_w_pubn[0],  g_csd_w_pubn[1],
                      g_csd_w_pubz[0],  g_csd_w_pubz[1],
                      g_csd_w_shipn[0], g_csd_w_shipn[1],
                      g_csd_w_shipz[0], g_csd_w_shipz[1],
                      g_csd_w_exch, g_csd_w_iter, g_csd_w_dsdn,
                      g_csd_w_portu[0], g_csd_w_portu[1],
                      csd_ups_diag_push_n(), csd_ups_diag_push_over(),
                      /* REVERT(s15-diag): see format block above. */
                      g_csd_cmd_tot, g_csd_cmd_sr, g_csd_cmd_start,
                      g_csd_cmd_stop, g_csd_cmd_unh, g_csd_cmd_last,
                      csd_ups_diag_rc_total(), csd_ups_diag_rc_site(0),
                      csd_ups_diag_rc_site(1), csd_ups_diag_rc_site(2),
                      csd_ups_diag_rc_site(3),
                      /* REVERT(s16-diag): block-granularity ring witnesses. */
                      csd_ups_diag_s16(0u), csd_ups_diag_s16(1u),
                      csd_ups_diag_s16(2u), csd_ups_diag_s16(3u),
                      csd_ups_diag_s16(4u), csd_ups_diag_s16(5u),
                      csd_ups_diag_s16(6u), csd_ups_diag_s16(7u),
                      csd_ups_diag_s16(8u), csd_ups_diag_s16(9u),
                      csd_ups_diag_s16(10u), csd_ups_diag_s16(11u),
                      /* REVERT(bp21): see format block above. */
                      g_csd_mir_pub_n, g_csd_mir_copy_n, g_csd_mir_hold_n,
                      /* REVERT(bp31): see format block above. */
                      g_csd_mir_relock_n, g_csd_mir_lag_n,
                      g_csd_flip_n, g_csd_tk_n,
                      g_csd_slot_n, g_csd_pub2_n,
                      s_rec_on_w, s_probe_pop_n_w,
                      s_feedcalls_task, s_feedcalls_audio,
                      csd_probe_en_get(),
                      g_csd_fb1, g_csd_fb2,
                      csd_ups_dbg_pushn(), csd_ups_dbg_fidx(),
                      g_csd_ft1, g_csd_ft2, g_csd_ft3,
                      g_csd_bp, g_csd_ft5, g_csd_ep0, g_csd_ep1, g_csd_ewz);
        /* REVERT(bench r8-rc): +29 discrimination fields (append-only). */
        {
            extern unsigned s_rc_midstream;
            extern unsigned s_occ_low[2];
            extern unsigned s_rep_run_max;
            extern unsigned s_pub_dup;
            extern unsigned s_rep_ph[2][8];
            extern unsigned s_off_hist[8];
            extern volatile unsigned g_csd_pop_phase_live;
            if (n > 0 && n < (int)sizeof(buf) - 12)
                n += snprintf(buf + n, sizeof(buf) - (unsigned)n,
                              ",%u,%u,%u,%u,%u",
                              s_rc_midstream, s_occ_low[0], s_occ_low[1],
                              s_rep_run_max, s_pub_dup);
            for (unsigned i = 0u; i < 16u && n < (int)sizeof(buf) - 12; i++)
                n += snprintf(buf + n, sizeof(buf) - (unsigned)n, ",%u",
                              (&s_rep_ph[0][0])[i]);
            for (unsigned i = 0u; i < 8u && n < (int)sizeof(buf) - 12; i++)
                n += snprintf(buf + n, sizeof(buf) - (unsigned)n, ",%u",
                              s_off_hist[i]);
            if (n < (int)sizeof(buf) - 12)
                n += snprintf(buf + n, sizeof(buf) - (unsigned)n, ",%u",
                              (unsigned)g_csd_pop_phase_live);
        }
                      /* REVERT(dp203): +iter,dsdn,portu.
                       * REVERT(dp202): dp197 dec_* chunk
                       * removed with its witnesses (decouple.xc reverted);
                       * tail field count 157 -> 147, verified on-device. */
    }
    if (n > 0) {
        if (n >= (int)sizeof(buf)) n = (int)sizeof(buf) - 1;   /* see above */
        SCPI_ResultCharacters(context, buf, n);
    }
    return SCPI_RES_OK;
}

/* ---- DIAG:UPSAMPLing:PHASE -- mic decimation phase (owner request 2026-11-09)
 *
 * "we need to have phase parameter to pick which sample to output during
 *  the downsample process ... for 48K fs we need to run bit-perfect test 8
 *  times with all 8 phase" -- the mic side decimates wire-rate slots to the
 * host rate once per `ratio` frames (the port decimator in
 * xua_audiohub.xc); this is the control that picks the slot. MIDI is the same
 * SCPI engine (SysEx carries the ASCII text through csd_scpi_begin/put/end),
 * so these two rows are the MIDI surface too -- no separate MIDI parsing, by
 * design.
 *
 * Set: DIAG:UPSAMPLing:PHASE <0..7> (8 = back to AUTO). Validated TWICE: the
 * literal range by gen_u32, then against the CURRENT live ratio -- phase
 * >= ratio is
 * rejected with -221 ILLEGAL_PARAMETER_VALUE (a phase that can never fire is
 * a rig bug waiting to read as a silent pass, the vacuous-pass shape this
 * project has been bitten by twice). The host tells rejected from accepted
 * by SYSTem:ERRor:NEXT? (empty queue == accepted) and always by
 * DIAG:UPSAMPLing:PHASE? re-reading what is in effect. The file's own
 * constraint applies: no free-form reason string (device-dependent error
 * info is off -- see push_hw_error), so the code plus the readback is the
 * contract. A successful set replies "phase,ratio" so a one-shot command
 * still confirms the accepted pair.
 *
 * ratio == 1 (stock capture): only phase 0 is settable and even it is
 * inert -- the per-slot compare in the audiohub runs against
 * CSD_MIC_SLOT_NEVER at ratio 1 (the boot value and the ratio-setter reset;
 * the owner compute constraint allows only a load + compare per slot there,
 * never a taken branch), and the transfer-point overwrite is guarded by
 * g_csd_usb_ratio > 1u, so the banked bit-perfect capture results are
 * untouched whether or not this command was ever used.
 *
 * REVERT: delete these two callbacks and their two table rows. */
static SCPI_FPTR_CMD scpi_result_t ups_phase_set(scpi_t *context)
{
    const char *why = "phase";
    uint32_t v;
    /* 0..7 = latch that slot; 8 = return to AUTO (the default). 8 is not a
     * phase -- it is the reset token, so the "phase >= ratio rejected" rule
     * applies to 0..7 only. Any other spelling fails gen_u32. */
    if (!gen_u32(context, "phase", &v, 0u, 8u, &why)) {
        (void)why;
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
    const unsigned ratio = xua_csd_get_usb_ratio();
    if (v < 8u && v >= ratio) {
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
    xua_csd_set_mic_phase(v);   /* >= 8 clamps to AUTO in the audiohub setter */
    char buf[24];
    /* (unsigned) casts: xcore's uint32_t is unsigned long, and %u with a
     * long argument drew -Wformat here; the reply digits are unchanged. */
    int n = snprintf(buf, sizeof(buf), "%u,%u", (unsigned)v, ratio);
    if (n > 0) SCPI_ResultCharacters(context, buf, n);
    return SCPI_RES_OK;
}

/* Query: DIAG:UPSAMPLing:PHASE? -> "phase,ratio,mode".
 * phase is the EFFECTIVE slot: an unset (AUTO) phase is reported as ratio-1
 * -- the slot it actually latches, which is today's implicit decimation --
 * so the reply answers "what is being captured right now", not "what was
 * last typed". mode separates auto (0) from operator-set (1) without
 * changing the phase field's meaning. REVERT: delete with its row. */
static SCPI_FPTR_CMD scpi_result_t ups_phase_q(scpi_t *context)
{
    const unsigned ratio = xua_csd_get_usb_ratio();
    unsigned ph = xua_csd_get_mic_phase();
    const unsigned mode = (ph == CSD_MIC_PHASE_AUTO) ? 0u : 1u;
    if (ph == CSD_MIC_PHASE_AUTO) ph = (ratio > 0u) ? (ratio - 1u) : 0u;
    char buf[24];
    int n = snprintf(buf, sizeof(buf), "%u,%u,%u", ph, ratio, mode);
    if (n > 0) SCPI_ResultCharacters(context, buf, n);
    return SCPI_RES_OK;
}

/* 2026-11-12 probe rings (see csd_ups_block.c): the only bit-exact bench
 * capture at ratio>1 — the mic echo path is resampled by ALSA (1-in-3
 * real pairs + interpolation garbage). PROBE <0|1> arms+resets; POP? and
 * FEED? dump CSV chunks (offset,count<=512). REVERT with the rings. */
extern void     csd_probe_enable(unsigned on);
extern unsigned csd_probe_base(void);
extern unsigned csd_probe_reconf(void);
extern unsigned csd_probe_reconf(void);
extern unsigned csd_probe_fidx(void);
extern unsigned csd_probe_ev_count(void);
extern unsigned csd_probe_en_get(void);
extern unsigned csd_probe_prepop(void);
extern unsigned csd_probe_feedabs(void);
extern unsigned csd_probe_armfidx_get(void);
extern unsigned csd_probe_snap_n(void);
extern unsigned csd_probe_snap_dataw(void);
extern unsigned csd_probe_snap_ringsz(void);
extern volatile unsigned s_snap_qaddr, s_snap_tile, s_feed_qaddr, s_feed_tile;
unsigned csd_probe_mis0(void);
unsigned csd_probe_mis1(void);
unsigned csd_probe_blkdiff(void);
unsigned csd_probe_qpop1(unsigned i);
extern unsigned s_probe_blkpop[8];
extern unsigned s_probe_blk_n;
extern unsigned s_probe_c0pop;
extern unsigned s_probe_reconfpop;
extern unsigned s_probe_headc;
/* dp144 REVERT: post-accept boundary witnesses (csd_ups_block.c) */
extern unsigned s_probe_bnd_feed[2], s_probe_bnd_pop[2];
extern unsigned s_probe_bnd_wc0[2], s_probe_bnd_wc1[2];
extern unsigned s_probe_bnd_head[2], s_probe_bnd_fa[2];
extern unsigned s_probe_bnd_s0[2], s_probe_bnd_s1[2], s_probe_bnd_s2[2];
extern unsigned s_probe_bnd_s3[2], s_probe_bnd_s15[2];
extern unsigned s_probe_bnd_done;
extern unsigned s_probe_bnd_scrc[2][4], s_probe_bnd_bcrc[2];
extern unsigned s_probe_bnd_pblob, s_probe_bnd_pdata, s_probe_bnd_pscratch;
extern volatile unsigned s_diag_qb[2], s_diag_pops_claim[2];        /* dp150 */
extern volatile unsigned s_diag_popn_claim, s_diag_kabs_claim;      /* dp150 */
extern volatile unsigned s_diag_pops_accept[2], s_diag_popn_accept; /* dp150 */
extern unsigned s_probe_datac;
extern unsigned s_probe_feedoff;
extern volatile unsigned s_probe_snap_body_crc, s_probe_snap_body_nz;   /* dp111 REVERT */
extern volatile unsigned s_probe_ring_direct_crc, s_probe_ring_direct_nz; /* dp113 REVERT */
extern volatile unsigned s_snap_noowner;   /* dp114b REVERT */
extern volatile unsigned s_probe_ring_pre_freeze_crc, s_probe_ring_pre_freeze_nz; /* dp117 */
extern volatile unsigned s_witness_sp_slot, s_witness_sp_feed, s_witness_sp_snap; /* dp120 */
extern volatile unsigned s_witness_stptr, s_witness_heapok, s_witness_fidx_cap, s_witness_winw0; /* dp122 */
extern volatile unsigned ups_heap_fail, ups_heap_total, probe_heap_total; /* dp126 */
extern volatile unsigned s_snap_progress, s_snap_entry; /* dp129 */
extern volatile unsigned s_snap_crc_capture, s_snap_crc_diag; /* dp130 */
extern volatile unsigned s_wit_g[4], s_snap_accepts; /* dp135 */
unsigned csd_ups_bypass_witness(void);
unsigned csd_ups_enabled_witness(void);
extern unsigned s_probe_wchk;
extern unsigned s_probe_wchk_snap;
extern unsigned s_probe_wchk_run;
extern unsigned s_feedcalls_task;
extern unsigned s_feedcalls_audio;
extern unsigned csd_probe_snap_word0(void);
extern unsigned csd_probe_snap_calls_get(void);
extern unsigned csd_probe_snap_dump(unsigned off, unsigned cnt);
extern unsigned csd_probe_ev_dump(unsigned off, unsigned cnt);
extern unsigned csd_probe_pre(void);
extern unsigned csd_probe_dump(unsigned off, unsigned cnt, int is_pop);
extern const char *csd_probe_dump_buf(void);
extern unsigned csd_probe_popbase(void);
extern unsigned csd_probe_pop_count(void);
extern unsigned csd_probe_feed_count(void);
extern int      csd_probe_pop_at(unsigned i, int out[2]);
extern int      csd_probe_feed_at(unsigned i, int out[2]);

extern unsigned s_rec_force;  /* REVERT(bench r8) */
static SCPI_FPTR_CMD scpi_result_t ups_probe_set(scpi_t *context)
{
    uint32_t on = 0u;
    const char *why = NULL;
    if (!gen_u32(context, "on", &on, 0u, 1u, &why)) {
        (void)why;
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
    csd_probe_enable(on);
    return SCPI_RES_OK;
}

/* Shared chunked CSV dump: DIAG:UPSAMPLing:<POP|FEED>? <off>,<count> */
static SCPI_FPTR_CMD scpi_result_t ups_probe_q(scpi_t *context,
                                               int is_pop)
{
    uint32_t off = 0u, cnt = 0u;
    const char *why = NULL;
    if (!gen_u32(context, "off", &off, 0u, 8191u, &why) ||
        !gen_u32(context, "count", &cnt, 1u, 512u, &why)) {
        (void)why;
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
    const unsigned o = csd_probe_dump(off, cnt, is_pop);
    if (o > 0) SCPI_ResultCharacters(context, csd_probe_dump_buf(), (int)o);
    return SCPI_RES_OK;
}

unsigned csd_probe_dump_qpop2(unsigned off, unsigned cnt);
const char *csd_probe_dump_qpop2_buf(void);
unsigned csd_probe_dump_qpop2_null(void);
unsigned csd_probe_dump_qpop2_n(void);
static SCPI_FPTR_CMD scpi_result_t ups_qpop2_q(scpi_t *context)
{
    uint32_t off = 0u, cnt = 0u;
    const char *why = NULL;
    if (!gen_u32(context, "off", &off, 0u, 8191u, &why) ||
        !gen_u32(context, "count", &cnt, 1u, 512u, &why)) {
        (void)why;
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
    const unsigned o = csd_probe_dump_qpop2(off, cnt);
    if (o > 0) { SCPI_ResultCharacters(context, csd_probe_dump_qpop2_buf(), (int)o); return SCPI_RES_OK; }
    /* Empty dump must never be silent: an empty body was indistinguishable
     * from a dead transport (bench 2026-11-17/18, hours lost). Name the gate
     * that refused, task-core only, non-numeric so no parser reads it as a
     * row. pop=1 alloc'd, n=pop ring count, en=enable stat (2 = OOM). */
    {
        extern unsigned csd_probe_en_get(void);
        static char why[64];
        snprintf(why, sizeof why, "GATE:pop=%u,n=%u,en=%u,off=%u",
                 csd_probe_dump_qpop2_null(), csd_probe_dump_qpop2_n(),
                 csd_probe_en_get(), off);
        SCPI_ResultCharacters(context, why, (int)strlen(why));
    }
    return SCPI_RES_OK;
}

/* ---- boot-flash error log (bench 2026-11-18/19) -------------------------
 * The board re-enumerates on USB every ~10 min (host -110) and RAM witnesses
 * die with it; these commands read what the codec server journalled to the
 * data partition before the death. ALL state and flash work is tile[0]'s
 * (csd_errlog.c architecture note): sector loads and every status/data word
 * ride the audiohw-diag channel as SINGLE scalars (the multi-word FMT
 * protocol deadlocked the codec server, 2026-11-19 — scalars only, decode
 * here); tile[1] posts ride the P0..P3/PGO ladder. */
#define ERRL_SLOTS_PER_SECTOR 128u
static char s_errlog_buf[6144];                  /* static: qpop2 precedent */

static SCPI_FPTR_CMD scpi_result_t diag_errlog_q(scpi_t *context)
{
    unsigned off = 0u, cnt = 8u; const char *why = NULL;
    if (!gen_u32(context, "off", &off, 0u, 1023u, &why) ||
        !gen_u32(context, "cnt", &cnt, 1u, 2u, &why)) {
        (void)why;
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
    /* Window protocol: ERRLOG_RSEC loads a sector into tile[0]'s cache;
     * STAT words 8..23 are its first 16 words (= slots 0 and 1). cnt is
     * capped at 2: the pair sharing one load. Validation is the rig-side
     * twin of el_slot_ok (csd_errlog.c): magic|nonce echo + XOR checksum. */
    unsigned n = 0u, emitted = 0u;
    for (unsigned k = 0u; k < cnt; k++) {
        unsigned idx = off + k;
        unsigned sec = idx / ERRL_SLOTS_PER_SECTOR;
        unsigned slot = idx % ERRL_SLOTS_PER_SECTOR;
        if (sec >= 8u) break;
        if ((slot & 1u) == 0u &&
            csd_diag_errlog_rsec(sec * 64u + (slot >> 1)) == 0u) break;
        unsigned w[8];
        for (unsigned j = 0u; j < 8u; j++)
            w[j] = csd_diag_errlog_status(8u + (slot & 1u) * 8u + j);
        unsigned cs = (w[1] ^ w[2] ^ w[3] ^ w[4] ^ w[5] ^ w[6]) & ~0xFFu;
        if (w[0] != (0x4345524Cu | (w[7] & 0xFFu))) continue;
        if ((w[7] & ~0xFFu) != cs) continue;
        int m = snprintf(s_errlog_buf + n,
                         (unsigned)sizeof s_errlog_buf - n,
                         "%u,%u,%u,%u,%u,%u;",
                         w[1], w[2], w[3], w[4], w[5], w[6]);
        if (m <= 0 || (unsigned)m >= (unsigned)sizeof s_errlog_buf - n) break;
        n += (unsigned)m;
        emitted++;
    }
    s_errlog_buf[n] = '\0';
    SCPI_ResultCharacters(context, s_errlog_buf, (int)n);
    return (int)emitted != 0 ? SCPI_RES_OK : SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t diag_errlog_stat_q(scpi_t *context)
{
    char buf[768];                    /* 36 witness words fit (statics rule) */
    unsigned len = 0u;
    /* Layout (csd_errlog.h): 0..7 ring status, 8..23 raw cache window,
     * 24..28 write-verify, 30..34 FIFO truth. */
    for (unsigned i = 0u; i < 36u; i++) {
        unsigned v = csd_diag_errlog_status(i);
        char tmp[12];
        int n = snprintf(tmp, sizeof tmp, (i ? ",%u" : "%u"), v);
        if (n <= 0 || (unsigned)n >= (unsigned)sizeof buf - len) break;
        memcpy(buf + len, tmp, (unsigned)n);
        len += (unsigned)n;
    }
    SCPI_ResultCharacters(context, buf, (int)len);
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t diag_errlog_test(scpi_t *context)
{
    unsigned id = 0u, a1 = 0u, a2 = 0u; const char *why = NULL;
    if (!gen_u32(context, "id", &id, 0u, 0xFFFFFFFFu, &why) ||
        !gen_u32(context, "a1", &a1, 0u, 0xFFFFFFFFu, &why) ||
        !gen_u32(context, "a2", &a2, 0u, 0xFFFFFFFFu, &why)) {
        (void)why;
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
    /* tile[1] posts ONLY via the ladder: four stage words + commit, each a
     * proven 1-word-in scalar command (tile-affinity rule, csd_errlog.c). */
    csd_diag_errlog_post4(EVL_EV_TEST, id, a1, a2);
    return SCPI_RES_OK;                            /* persistence proven by
                                                    * reading it back later */
}

static SCPI_FPTR_CMD scpi_result_t diag_errlog_erase_cmd(scpi_t *context)
{
    if (csd_diag_errlog_erase() != 1u) {
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
        return SCPI_RES_ERR;
    }
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t ups_recforce_set(scpi_t *context)
{   unsigned v=0; const char *why=NULL;
    extern unsigned s_rec_force;
    if (!gen_u32(context, "val", &v, 0u, 1u, &why)) { (void)why;
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE); return SCPI_RES_ERR; }
    s_rec_force = v; return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t ups_base_q(scpi_t *context)
{
    char buf[2400];   /* dp144/dp150/dp172: witness tail (122 fields) */
    /* dp112: EXACT count — 33 originals + body_crc + body_nz = 35.
     * dp110 shipped 34 %u for 33 args: a past-the-end read that fed the
     * host a garbage tail field (384000 / 2409399996). Field-map from
     * source, always. */
    snprintf(buf, sizeof buf, "%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u",   /* dp135: 61 */
             csd_probe_base(), csd_probe_popbase(),
             csd_probe_reconf(), csd_probe_pre(), csd_probe_fidx(),
             csd_probe_en_get(), csd_probe_prepop(),
             csd_probe_feedabs(),                               csd_probe_armfidx_get(),
                               csd_probe_snap_word0(), csd_probe_snap_n(),
                               csd_probe_snap_dataw(),
                               s_snap_qaddr, s_snap_tile, s_feed_qaddr, s_feed_tile,
                               csd_probe_mis0(), csd_probe_mis1(),
                               csd_probe_blkdiff(),
                               s_probe_blk_n ? s_probe_blkpop[0] : 99999u,
                               s_probe_blk_n > 1u ? s_probe_blkpop[1] : 99999u,
                               s_probe_blk_n > 2u ? s_probe_blkpop[2] : 99999u,
                               s_probe_blk_n > 3u ? s_probe_blkpop[3] : 99999u,
                               s_probe_c0pop,
                               s_probe_reconfpop, s_probe_headc,
                               s_probe_datac, s_probe_feedoff,
                               s_probe_wchk, s_probe_wchk_snap,
                               s_probe_wchk_run, s_feedcalls_task,
                               s_feedcalls_audio,
                               s_probe_snap_body_crc, s_probe_snap_body_nz,
                               s_probe_ring_direct_crc, s_probe_ring_direct_nz,
                               s_snap_noowner,
                               s_probe_ring_pre_freeze_crc, s_probe_ring_pre_freeze_nz,
                               s_witness_sp_slot, s_witness_sp_feed, s_witness_sp_snap,
                               s_witness_stptr, s_witness_heapok, s_witness_fidx_cap, s_witness_winw0,
                               ups_heap_fail, ups_heap_total, probe_heap_total,
                               csd_ups_bypass_witness(), csd_ups_enabled_witness(),
                               s_snap_progress, s_snap_entry,
                               s_snap_crc_capture, s_snap_crc_diag,
                               s_wit_g[0], s_wit_g[1], s_wit_g[2], s_wit_g[3],
                               s_snap_accepts);
    /* dp144 REVERT: boundary witnesses appended as fields 61..83 (tails
     * never shift existing indices — the host field map stays valid). */
    {
        int n = (int)strlen(buf);
        snprintf(buf + n, sizeof buf - (size_t)n,
                 ",%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u"
                 ",%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u",
                 s_probe_bnd_done,
                 s_probe_bnd_feed[0], s_probe_bnd_pop[0],
                 s_probe_bnd_wc0[0], s_probe_bnd_wc1[0], s_probe_bnd_head[0],
                 s_probe_bnd_fa[0], s_probe_bnd_s0[0], s_probe_bnd_s1[0],
                 s_probe_bnd_s2[0], s_probe_bnd_s3[0], s_probe_bnd_s15[0],
                 s_probe_bnd_feed[1], s_probe_bnd_pop[1],
                 s_probe_bnd_wc0[1], s_probe_bnd_wc1[1], s_probe_bnd_head[1],
                 s_probe_bnd_fa[1], s_probe_bnd_s0[1], s_probe_bnd_s1[1],
                 s_probe_bnd_s2[1], s_probe_bnd_s3[1], s_probe_bnd_s15[1],
                 /* dp146 REVERT: bisection + alignment fields 84..96 */
                 s_probe_bnd_scrc[0][0], s_probe_bnd_scrc[0][1],
                 s_probe_bnd_scrc[0][2], s_probe_bnd_scrc[0][3],
                 s_probe_bnd_scrc[1][0], s_probe_bnd_scrc[1][1],
                 s_probe_bnd_scrc[1][2], s_probe_bnd_scrc[1][3],
                 s_probe_bnd_bcrc[0], s_probe_bnd_bcrc[1],
                 s_probe_bnd_pblob, s_probe_bnd_pdata, s_probe_bnd_pscratch);
        /* dp150 REVERT: qpop1 row-map diagnostics, fields 97..105:
         * qb0 qb1 pops0 pops1 popn kabs at claim; pops0 pops1 popn at
         * the accept reset. */
        n = (int)strlen(buf);
        snprintf(buf + n, sizeof buf - (size_t)n,
                 ",%u,%u,%u,%u,%u,%u,%u,%u,%u",
                 s_diag_qb[0], s_diag_qb[1],
                 s_diag_pops_claim[0], s_diag_pops_claim[1],
                 s_diag_popn_claim, s_diag_kabs_claim,
                 s_diag_pops_accept[0], s_diag_pops_accept[1],
                 s_diag_popn_accept);
    }
    SCPI_ResultCharacters(context, buf, (unsigned)strlen(buf));
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t ups_pop_q(scpi_t *context)
{ return ups_probe_q(context, 1); }

static SCPI_FPTR_CMD scpi_result_t ups_qpop_q(scpi_t *context)
{ return ups_probe_q(context, 2); }

static SCPI_FPTR_CMD scpi_result_t ups_state_q(scpi_t *context)
{
    uint32_t off = 0u, cnt = 0u;
    const char *why = NULL;
    if (!gen_u32(context, "off", &off, 0u, 65535u, &why) ||
        !gen_u32(context, "count", &cnt, 1u, 384u, &why)) {
        (void)why;
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
    const unsigned w0 = csd_probe_snap_word0();
    const unsigned dw = csd_probe_snap_dataw();
    const unsigned n = (w0 >= 1u && w0 <= 8u && (w0 & (w0 - 1u)) == 0u)
        ? 264u + csd_probe_snap_ringsz() + 2u * w0 * dw : 0u;
    char hdr[48];
    int hl = snprintf(hdr, sizeof hdr, "N=%u,C=%u;", n,
                           csd_probe_snap_calls_get());
    (void)hl;
    SCPI_ResultCharacters(context, hdr, (unsigned)strlen(hdr));
    const unsigned o = csd_probe_snap_dump(off, cnt);
    if (o > 0) SCPI_ResultCharacters(context, csd_probe_dump_buf(), (int)o);
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t ups_ev_q(scpi_t *context)
{
    uint32_t off = 0u, cnt = 0u;
    const char *why = NULL;
    if (!gen_u32(context, "off", &off, 0u, 8191u, &why) ||
        !gen_u32(context, "count", &cnt, 1u, 400u, &why)) {
        (void)why;
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
    const unsigned n = csd_probe_ev_count();
    char hdr[32];
    int hl = snprintf(hdr, sizeof hdr, "N=%u;", n);
    (void)hl;
    SCPI_ResultCharacters(context, hdr, (unsigned)strlen(hdr));
    const unsigned o = csd_probe_ev_dump(off, cnt);
    if (o > 0) SCPI_ResultCharacters(context, csd_probe_dump_buf(), (int)o);
    return SCPI_RES_OK;
}
static SCPI_FPTR_CMD scpi_result_t ups_feed_q(scpi_t *context)
{ return ups_probe_q(context, 0); }

/* 11.11.36 mirror-select (REVERT with the tap): cold path only. */
extern unsigned g_csd_ups_echo_mode;
static SCPI_FPTR_CMD scpi_result_t ups_echo_set(scpi_t *context)
{
    uint32_t mode = 0u;
    const char *why = NULL;
    if (!gen_u32(context, "mode", &mode, 0u, 2u, &why)) {  /* 2 (dp158): ADC passthrough */
        (void)why;
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;
    }
    g_csd_ups_echo_mode = mode;
    return SCPI_RES_OK;
}

static SCPI_FPTR_CMD scpi_result_t ups_set(scpi_t *context)
{
    uint32_t enable = 0u;
    const char *why = NULL;                 /* gen_u32's reason string */
    if (!gen_u32(context, "state", &enable, 0u, 1u, &why)) {
        (void)why;
        SCPI_ErrorPush(context, SCPI_ERROR_ILLEGAL_PARAMETER_VALUE);
        return SCPI_RES_ERR;                /* the file's own idiom */
    }
    unsigned ap = 0u, sf = 0u, rf = 0u;
#if defined(CSD_RELEASE) && CSD_RELEASE
    /* RELEASE image: the resampler is pinned disarmed (csd_ups_block.c pins
     * want/enabled/active/armed to 0), so the command is ACCEPTED and
     * answered with the true state -- which stays "off" -- and touches
     * neither the codec, the journal nor the engine. A release board cannot
     * be armed into the DSP path; nothing else about the engine is stubbed. */
    (void)enable; (void)ap; (void)sf; (void)rf;
    ups_reply(context);
    return SCPI_RES_OK;
#endif
    /* persist=1: an operator command is the only thing that journals the
     * setting. A rate-change re-grant goes through the codec path with 0. */
    if (!csd_diag_ups_set(enable, 1u, &ap, &sf, &rf)) {
        SCPI_ErrorPush(context, SCPI_ERROR_EXECUTION_ERROR);
        return SCPI_RES_OK;
    }
    /* Mirror into the audio path only what the codec actually accepted, so the
     * DSP can never be upsampling while the chip still has its own FIR on. */
    /* Keep the tile[1] intent in step with the operator's command at once;
     * the journal is the authority and the next stream start re-reads it. */
    /* Intent is the operator's, not the codec's: set it from the command so
     * the next stream start can force the rate and the grant can then land. */
    csd_ups_set_want(enable);
    csd_ups_set_enabled(ap);
    /* This handler is the diag core (tile[1]) and owns the audiohw-diag
     * channel: the ARM/ENABLE journal rides the scalar ladder (tile-affinity
     * rule, csd_errlog.c). */
    csd_diag_errlog_post4(enable ? EVL_EV_ARM : EVL_EV_DISARM, ap, 0u, 0u);
    if (ap) csd_diag_errlog_post4(EVL_EV_ENABLED, 1u, 0u, 0u);
    ups_reply(context);
    return SCPI_RES_OK;
}

/* REVERT(dp183): runtime attribution probe — mask setter/query. bit0 skips
 * the two csd_ups_slot pop calls in the audiohub wire loop; bit1 skips the
 * top-of-loop wire floor. Also echoes the (persistent, global) upsampler
 * ECHO mode because DIAG:UPSAMPLing:ECHO has no query handler. */
unsigned g_csd_probe_mask = 0u;
extern unsigned g_csd_ups_echo_mode;
static SCPI_FPTR_CMD scpi_result_t dbg_probe_set(scpi_t *context)
{
    int32_t v;
    if (!SCPI_ParamInt32(context, &v, true)) return SCPI_RES_ERR;
    g_csd_probe_mask = (unsigned)v;
    return SCPI_RES_OK;
}
static SCPI_FPTR_CMD scpi_result_t dbg_probe_q(scpi_t *context)
{
    char b[32];
    int n = snprintf(b, sizeof b, "%u,%u", g_csd_probe_mask,
                     g_csd_ups_echo_mode);
    if (n < 0) n = 0;
    if (n >= (int)sizeof(b)) n = (int)sizeof(b) - 1;
    SCPI_ResultCharacters(context, b, n);
    return SCPI_RES_OK;
}

/* REVERT(dp185): segment timing readout. seg[0]=L ADC read+clk emit,
 * 1=R ADC read+clk emit, 2=DAC-L claim, 3=DAC-R claim, 4=floor block,
 * 5=exchange block; 6=producer pass, 7=produce_pair. All in 10 ns ticks,
 * accumulated; seg_n = wire frames counted while mask bit4 was set. */
static SCPI_FPTR_CMD scpi_result_t dbg_seg_q(scpi_t *context)
{
    /* REVERT(dp199-attr): appended seg_b[0..3] + loop_ticks (append-only;
     * the old 9 fields keep their positions). */
    extern unsigned g_csd_seg[8], g_csd_seg_n;
    extern unsigned g_csd_seg_b[6], g_csd_loop_ticks;
    extern unsigned csd_gf_n_get(void);
    extern void csd_lr_wit_get(unsigned*,unsigned*,unsigned*,unsigned*);
    unsigned lmn[2], lmx[2], lsm[2], ln[2];
    csd_lr_wit_get(lmn, lmx, lsm, ln);
    char b[512];
    int n = snprintf(b, sizeof b,
                     "%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u",
                     g_csd_seg[0], g_csd_seg[1], g_csd_seg[2], g_csd_seg[3],
                     g_csd_seg[4], g_csd_seg[5], g_csd_seg[6], g_csd_seg[7],
                     g_csd_seg_n,
                     g_csd_seg_b[0], g_csd_seg_b[1], g_csd_seg_b[2],
                     g_csd_seg_b[3], g_csd_loop_ticks,
                     g_csd_seg_b[4], g_csd_seg_b[5], csd_gf_n_get(),
                     lmn[0], lmx[0], lsm[0], ln[0], lmn[1], lmx[1],
                     lsm[1], ln[1]);
    if (n < 0) n = 0;
    if (n >= (int)sizeof(b)) n = (int)sizeof(b) - 1;
    SCPI_ResultCharacters(context, b, n);
    return SCPI_RES_OK;
}
static SCPI_FPTR_CMD scpi_result_t dbg_seg_clear(scpi_t *context)
{
    extern unsigned g_csd_seg[8], g_csd_seg_n;
    for (int i = 0; i < 8; i++) g_csd_seg[i] = 0;
    g_csd_seg_n = 0;
    /* REVERT(dp199-attr): clear the appended bank too. */
    extern unsigned g_csd_seg_b[6], g_csd_loop_ticks;
    extern void csd_gf_n_clear(void);
    extern void csd_lr_wit_clear(void);
    for (int i = 0; i < 6; i++) g_csd_seg_b[i] = 0;
    g_csd_loop_ticks = 0;
    csd_gf_n_clear();
    csd_lr_wit_clear();
    return SCPI_RES_OK;
}

/* REVERT(dp177): dedicated pacer probe — unambiguous named readout of the
 * live claim/floor pacing state (positional INFO tail proved error-prone). */
static SCPI_FPTR_CMD scpi_result_t ups_pace_q(scpi_t *context)

{
    extern unsigned g_csd_f_wnext, g_csd_f_now2, g_csd_f_wt, g_csd_f_add_n;
    extern unsigned g_csd_claim_wnext, g_csd_claim_now, g_csd_claim_stp;
    extern unsigned g_csd_claim_pace_n, g_csd_p_ptk, g_csd_p_pnext;
    extern unsigned g_csd_ps_seq, g_csd_ps_consumed;
    extern unsigned g_csd_ps_mbox_seq;
    extern unsigned g_csd_ps_fresh; /* REVERT(dp196) */
    extern unsigned g_csd_ps_starve, g_csd_ps_active;   /* REVERT(dp196) */
    extern unsigned g_csd_wire_l0, g_csd_wire_seen, g_csd_wire_s1,
                    g_csd_wire_s2, g_csd_wire_n;         /* REVERT(dp196) */
    extern unsigned g_csd_prod_w0, g_csd_prod_n, g_csd_prod_tile,
                    g_csd_wire_tile;   /* REVERT(dp196) */
    extern unsigned g_csd_p_err, g_csd_p_dwl, g_csd_p_ptkxi; /* REVERT(dp198) */
    extern unsigned g_csd_p_tdl, g_csd_p_pndl;               /* REVERT(dp198) */
    /* REVERT(dp201): producer-pass attribution (csd_ups_block.c,
     * csd_ps_ring.c; PROBE bit4 gated accumulators, fixed-window deltas). */
    extern unsigned g_csd_p_halt, g_csd_p_halt_n, g_csd_p_ifed, g_csd_p_ifed_n;
    extern unsigned g_csd_p_snap, g_csd_p_snap_n, g_csd_p_claim, g_csd_p_claim_n;
    extern unsigned g_csd_p_pop2, g_csd_p_passn, g_csd_p_ubm, g_csd_p_ubmn;
    /* REVERT(dp201): tile[1] EQ task freeze attribution (csd_ups_block.c). */
    extern unsigned g_csd_eq_stage, g_csd_eq_stage_t;
    extern unsigned g_csd_eq_claim_rounds, g_csd_eq_claim_exit;
    extern unsigned g_csd_live_pop_in_commit, g_csd_live_snap_freeze;
    extern unsigned g_csd_live_in_feed, g_csd_live_probe_popn;
    char buf[640];   /* REVERT(dp201): 50 fields — widened from 384 */
    int n = snprintf(buf, sizeof(buf),
                     "%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,"
                     "%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,"
                     "%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,"
                     "%u,%u,%u,%u,%u,%u,%u,%u",
                     g_csd_f_wt, g_csd_f_wnext, g_csd_f_now2, g_csd_f_add_n,
                     g_csd_claim_stp, g_csd_claim_wnext, g_csd_claim_now,
                     g_csd_claim_pace_n, g_csd_p_ptk, g_csd_p_pnext,
                     g_csd_ps_seq, g_csd_ps_consumed,
                     g_csd_ps_mbox_seq, g_csd_ps_fresh,
                     g_csd_ps_starve, g_csd_ps_active,
                     g_csd_wire_l0, g_csd_wire_seen, g_csd_wire_s1,
                     g_csd_wire_s2, g_csd_wire_n,
                     g_csd_prod_w0, g_csd_prod_n, g_csd_prod_tile,
                     g_csd_wire_tile,
                     g_csd_p_err, g_csd_p_dwl, g_csd_p_ptkxi,
                     g_csd_p_tdl, g_csd_p_pndl,
                     g_csd_p_halt, g_csd_p_halt_n, g_csd_p_ifed, g_csd_p_ifed_n,
                     g_csd_p_snap, g_csd_p_snap_n, g_csd_p_claim, g_csd_p_claim_n,
                     g_csd_p_pop2, g_csd_p_passn, g_csd_p_ubm, g_csd_p_ubmn,
                     g_csd_eq_stage, g_csd_eq_stage_t,
                     g_csd_eq_claim_rounds, g_csd_eq_claim_exit,
                     g_csd_live_pop_in_commit, g_csd_live_snap_freeze,
                     g_csd_live_in_feed, g_csd_live_probe_popn);
    if (n < 0) n = 0;
    if (n >= (int)sizeof(buf)) n = (int)sizeof(buf) - 1;
    SCPI_ResultCharacters(context, buf, n);
    return SCPI_RES_OK;
}

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

    /* Host-side kernel aperture for the single biquad section (see
     * coef_set's block note). Query and set halves are separate entries;
     * the DEFault form restores the SHIPPED DEFAULT -- unity, not the
     * former shelf. matchCommand cannot let one of these shadow another
     * (a row whose pattern is exhausted while the header still has text
     * fails the match), so row order is not load-bearing. */
    { .pattern = "DIAG:DSP:COEFFicient?",        .callback = coef_q       },
    { .pattern = "DIAG:DSP:COEFFicient:FORMat?", .callback = coef_format_q},
    { .pattern = "DIAG:DSP:COEFFicient:DEFault", .callback = coef_default },
    { .pattern = "DIAG:DSP:COEFFicient",         .callback = coef_set     },
    /* Witness-window control and transport loss counters (see the handlers). */
    { .pattern = "DEBUG:DSP:CLEAR",              .callback = dsp_clear      },
    { .pattern = "DEBUG:REPLY?",                 .callback = reply_stats_q  },

    /* Unit identity. Queries only -- there is deliberately no setter and no
     * OTP write path. Patterns carry no parameter placeholders, same rule as
     * above: libscpi matches the pattern against the command HEADER only. */
    /* Boot-flash error log: reads the data-partition ring the codec server
     * appends to. Params are read with SCPI_Param* inside the callbacks,
     * never as placeholders in the pattern (rule above). */
    { .pattern = "DIAG:ERRLOG?",          .callback = diag_errlog_q       },
    { .pattern = "DIAG:ERRLOG:STAT?",     .callback = diag_errlog_stat_q  },
    { .pattern = "DIAG:ERRLOG:TEST",      .callback = diag_errlog_test    },
    { .pattern = "DIAG:ERRLOG:ERASE",     .callback = diag_errlog_erase_cmd },

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

    /* FIR preset select (two-button hold companion, 2026-11-16). */
    { .pattern = "DIAG:FILTer:STATe?", .callback = q_filter_state },
    { .pattern = "DIAG:FILTer:TOGgle", .callback = cmd_filter_toggle },

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
    /* Idle-power timeout (owner case 14): a production control, not a debug
     * probe -- the owner's spec requires a host to set and read it, so these
     * rows sit OUTSIDE the release gate, beside the trim rows for the same
     * reason. */
    { .pattern = "PERSISTence:SAVE",        .callback = persist_save    },
    { .pattern = "POWER:TIMEout",           .callback = power_timeout_set },
    { .pattern = "POWER:TIMEout?",          .callback = power_timeout_q   },
    /* One control for XMOS upsampling and the DAC's FIR bypass together.
     * Reply is "xmos_en,applied,frame_hz,codec_refusals,ups_refusals". */
    { .pattern = "DIAG:UPSAMPLing:STATe",   .callback = ups_set           },
    { .pattern = "DIAG:UPSAMPLing:ECHO",    .callback = ups_echo_set      },
    { .pattern = "DIAG:UPSAMPLing:PROBE",   .callback = ups_probe_set     },
    { .pattern = "DIAG:UPSAMPLing:RECFORCE",  .callback = ups_recforce_set  }, /* REVERT(bench r8) */


    { .pattern = "DIAG:UPSAMPLing:PACE?",   .callback = ups_pace_q        },
    /* REVERT(dp183): attribution probe (see handlers above). */
    { .pattern = "DEBUG:CSD:PROBE?",        .callback = dbg_probe_q       },
    { .pattern = "DEBUG:CSD:PROBE",         .callback = dbg_probe_set     },
    /* REVERT(dp185): segment timing readout (audiohub g_csd_seg). */
    { .pattern = "DEBUG:CSD:SEG?",          .callback = dbg_seg_q         },
    { .pattern = "DEBUG:CSD:SEG:CLEAR",     .callback = dbg_seg_clear     },
    { .pattern = "DIAG:UPSAMPLing:BASE?",   .callback = ups_base_q        },
    { .pattern = "DIAG:UPSAMPLing:POP?",    .callback = ups_pop_q         },
    { .pattern = "DIAG:UPSAMPLing:QPOP2?",   .callback = ups_qpop2_q       },
    { .pattern = "DIAG:UPSAMPLing:QPOP?",   .callback = ups_qpop_q        },
    { .pattern = "DIAG:UPSAMPLing:FEED?",   .callback = ups_feed_q        },
    { .pattern = "DIAG:UPSAMPLing:EV?",     .callback = ups_ev_q          },
    { .pattern = "DIAG:UPSAMPLing:STATE?",  .callback = ups_state_q       },
    { .pattern = "DIAG:UPSAMPLing:STATe?",  .callback = ups_q             },
    /* Read-only truth table (see ups_info_q): armed/enabled/want/rates/ratio
     * plus the pop side of the ring. New 2026-11-09; REVERT: delete this row. */
    { .pattern = "DIAG:UPSAMPLing:INFO?",   .callback = ups_info_q        },
    /* Host clock-request history (see ups_clk_q). REVERT: delete this row. */
    { .pattern = "DIAG:UPSAMPLing:CLK?",    .callback = ups_clk_q         },
    { .pattern = "DIAG:UPSAMPLing:DIVider?", .callback = ups_div_q        },
    /* Mic decimation phase (owner request 2026-11-09): set 0..ratio-1, read
     * effective phase + ratio + auto/operator mode. MIDI carries the same
     * text as SysEx -- see ups_phase_set. REVERT: delete these two rows. */
    { .pattern = "DIAG:UPSAMPLing:PHASE",   .callback = ups_phase_set     },
    { .pattern = "DIAG:UPSAMPLing:PHASE?",  .callback = ups_phase_q       },
    { .pattern = "VOLume:TRIM:LOAD",        .callback = vol_trim_load     },
    { .pattern = "VOLume:TRIM:SAVe",        .callback = vol_trim_save     },
    { .pattern = "VOLume:TRIM:RESET",       .callback = vol_trim_reset    },
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
#endif
#ifndef SPECIALTYCIRCUITS_RELEASE   /* reopened: rows below reference callbacks
                                     * that only exist in a debug build */
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
/* INPUT_BUF_LEN is defined once, up with REPLY_MAX (2026-11-10 kernel
 * container sizing) — a second 160-byte define here shadowed it for years
 * and capped every SET command at one short line. */
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
