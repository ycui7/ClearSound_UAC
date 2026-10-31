/* CSD unit identity -- the string/USB half. See csd_identity.h for the
 * provenance rules and why the module is split in two.
 *
 * READ-ONLY: no OTP programming here either. */
#include "csd_identity.h"

#include <stdint.h>
#include <string.h>

/* lib_xua's public setter for the USB iSerialNumber string. Declared here
 * rather than including xua_endpoint0.h to keep this file free of the USB
 * headers: the signature under __XC__ only differs by an unsafe qualifier,
 * and this translation unit is C. */
extern void XUA_Endpoint0_setSerialStr(char *serial_str);

/* ---- state ------------------------------------------------------------ */
/* One writer: csd_identity_report(), called once by the tile[0] boot task.
 * Readers are the diagnostic core. There is a window between the copy below
 * completing and a host string request, and it is benign in practice for one
 * reason worth stating: the USB device is not even connectable until XUD
 * init, and host enumeration reaches a string descriptor tens of ms later,
 * while this task runs at t=0. It is not made ATOMIC, and prettier claims
 * would be false. */
/* Marker for "not read yet". Deliberately NOT lowercase hex: a real serial is
 * 8 lowercase hex characters, so this can never be mistaken for one. Nor is
 * it "" -- lib_xua drops iSerialNumber from the descriptor entirely when the
 * string is empty, which would make the serial vanish rather than read blank.
 * Nor "00000000"/"123456789": those are what the unprovisioned factory image
 * answers, and reusing them would hide the difference between "our firmware
 * ran" and "our firmware never ran". */
#define CSD_ID_PENDING "IPENDING"

static unsigned          s_ready;
static unsigned          s_stage;   /* 0..6, how far the staged read got */
static unsigned          s_src;                       /* csd_id_source_t */
static unsigned          s_words[CSD_ID_MATERIAL_WORDS];
static unsigned long long s_hash;
static char              s_str[CSD_ID_SERIAL_LEN + 1];

/* ---- local formatting -------------------------------------------------- */

/* Lowercase hex, exactly `digits`, most significant first. Fixed width on
 * purpose: a minimal-digit formatter turns a leading-zero serial into a
 * shorter string, and a serial whose length depends on its value cannot be
 * validated by length or compared field-for-field. */
static void hex_fixed_lower(unsigned long long v, unsigned digits, char *out)
{
    static const char d[] = "0123456789abcdef";
    for (unsigned i = 0; i < digits; i++) {
        out[digits - 1u - i] = d[(v >> (4u * i)) & 0xFu];
    }
    out[digits] = 0;
}

/* The one display string, shared by the USB descriptor and the SCPI queries.
 * Two sources for "the serial" is how a device ends up reporting one value on
 * iSerial and another over *IDN?, which is precisely the inconsistency this
 * module had (SERial? returned an error while USB showed the marker). */
static const char *display_str(void)
{
    return s_ready ? s_str : CSD_ID_PENDING;
}

/* ---- handoff from the tile[0] read ------------------------------------- */
void csd_identity_report(unsigned otp_header,
                         unsigned otp_serial,
                         unsigned otp_board_id,
                         unsigned node0, unsigned node1,
                         unsigned jtag0, unsigned jtag1,
                         unsigned long long hash)
{
    /* Order is the contract in csd_identity.h. A host recomputes the hash
     * from exactly these words, so it is written once here and nowhere
     * else -- readers get a copy. */
    unsigned w[CSD_ID_MATERIAL_WORDS];
    w[0] = node0;
    w[1] = node1;
    w[2] = jtag0;
    w[3] = jtag1;
    w[4] = otp_header;
    w[5] = otp_serial;
    w[6] = otp_board_id;

    /* Build the whole answer before publishing any of it, so a reader can
     * never see a half-written serial. */
    unsigned src;

    /* An all-zero serial word does NOT count as a provisioned serial: a
     * header that claims a serial which reads back 0 is as unprovisioned as
     * no header at all, and calling it OTP would be a false claim of unit
     * identity on a bench record. */
    src = (otp_serial != 0u) ? CSD_ID_OTP : CSD_ID_DERIVED;

    /* The serial is the tail of the hash: the low 32 bits, 8 lowercase hex
     * characters, for every board whether the identity came from OTP or from
     * derived material. Provenance is NOT encoded in the serial -- it is
     * reported separately by DIAG:IDENTity:SOURce?. Encoding it here (an OT/DV
     * prefix) would make one field answer two questions and put non-hex
     * characters into a USB serial string.
     *
     * The tail is the whole hash modulo 2^32, so it is only 4 billion values:
     * fine for labelling a bench fleet, NOT sufficient as a warranty identity
     * across a large production run without a collision study. The full
     * 64-bit value remains available (DIAG:IDENTity:HASH?) and is what a
     * production record should carry. */
    char str[CSD_ID_SERIAL_LEN + 1];
    hex_fixed_lower(hash, CSD_ID_SERIAL_LEN, str);

    for (unsigned i = 0; i < CSD_ID_MATERIAL_WORDS; i++) s_words[i] = w[i];
    s_hash = hash;
    for (unsigned i = 0; i <= CSD_ID_SERIAL_LEN; i++) s_str[i] = str[i];
    s_src = src;
    s_ready = 1u;                             /* published last */

    /* The descriptor is updated by csd_identity_usb_publish(), called once by
     * the task -- NOT here. An earlier revision pushed from inside this
     * function and the task called it per stage, so a later partial snapshot
     * overwrote an earlier complete serial with zeros. One publish, after the
     * value has stopped changing. */
}

void csd_identity_usb_publish(void)
{
    /* Runs on tile[0], the tile that owns g_strTable (.dp.data, i.e. DP/CP
     * relative) and the tile XUA_Endpoint0 runs on -- so this copy needs no
     * channel and no cross-tile sharing. (It is also why the serial works for
     * USB while the SCPI queries, served on the tile-1 diagnostic core, read
     * a different copy of these statics.)
     *
     * Writes unconditionally, including the pending marker. Guarding on
     * non-empty was a bug: at entry s_str IS empty, so the entry call did
     * nothing and the descriptor kept lib_xua's factory "123456789" -- which
     * is exactly the signal that tells the thread ran from the signal that
     * says it never started.
     *
     * Never "" though: lib_xua removes iSerialNumber from the descriptor
     * entirely when the string is empty, so a blank write would make the
     * serial vanish rather than read blank. */
    XUA_Endpoint0_setSerialStr(s_ready ? s_str : (char *)CSD_ID_PENDING);
}

/* ---- readers ----------------------------------------------------------- */

void csd_identity_stage(unsigned stage)
{
    /* Monotonic by construction: the task only ever advances it. Written
     * before the corresponding report so a reader can never see stage 3 with
     * stage-2 data, nor the reverse. */
    if (stage > s_stage) s_stage = stage;
}

unsigned csd_identity_get_stage(void)
{
    return s_stage;
}

csd_id_source_t csd_identity_source(void)
{
    return s_ready ? (csd_id_source_t)s_src : CSD_ID_NONE;
}

unsigned csd_identity_is_ready(void)
{
    return s_ready;
}

unsigned csd_identity_material_word(unsigned k)
{
    if (!s_ready || k >= CSD_ID_MATERIAL_WORDS) return 0u;
    return s_words[k];
}

unsigned csd_identity_str_word(unsigned k)
{
    if (k >= CSD_ID_STR_WORDS) return 0u;
    /* Packs display_str(), NOT s_str. Packing s_str meant this returned all
     * zeros until the boot read finished, so DIAG:IDENTity:SERial? reported
     * an empty string as an error while the USB descriptor showed the pending
     * marker -- one state, two answers. Now both consumers get the same bytes.
     *
     * Pack 4 chars little-endian; bytes past the terminator read as 0, which
     * the unpacker on the other side treats as the terminator. */
    const char *s = display_str();
    unsigned w = 0;
    for (unsigned b = 0; b < 4u; b++) {
        unsigned idx = k * 4u + b;
        unsigned ch = (idx < CSD_ID_STR_CAP)
                    ? (unsigned)(unsigned char)s[idx] : 0u;
        w |= ch << (8u * b);
    }
    return w;
}

unsigned long long csd_identity_hash(void)
{
    return s_ready ? s_hash : 0ull;
}
