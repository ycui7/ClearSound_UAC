/* CSD unit identity -- the OTP/sswitch half. See csd_identity.h for the
 * provenance rules and why the module is split in two.
 *
 * READ-ONLY BY CONSTRUCTION. There is no otp_program* call in this file and
 * there must never be one: OTP burns 0->1 once and cannot be erased, so a
 * mis-provisioned serial is permanent. Provisioning belongs to xburn on the
 * production line, reviewed separately. otp3.h is included for the reads
 * only.
 *
 * WHY ITS OWN THREAD, AND WHY IT PUBLISHES IN STAGES.
 * An earlier revision ran the whole read from the top of
 * AudioHwRemoteTile0, before that task's select loop. It wedged the bench:
 * the identity read blocked, so the task never reached the loop that services
 * codec register access, so every REGister:READ? blocked behind it -- and
 * because there is a single SCPI core, the entire diagnostic interface went
 * dead with it, including commands that have nothing to do with the codec. A
 * bench feature that can take the audio path down is not a bench feature.
 *
 * So: it runs as its own thread, where a blocked read can only strand itself;
 * and it publishes after each stage, so whatever completed stays visible.
 * That also converts "the identity read hung" from a guess into a measurement
 * -- the material words reported by DIAG:IDENTity:RAW? show exactly how far
 * it got, which one run answers instead of a series of guesses. */
#include "csd_identity.h"

#include <xs1.h>
#include <xclib.h>
#include <platform.h>
#include <otp3.h>

/* ---- configuration ---------------------------------------------------- */

/* Board-info header search starts at OTP virtual address 0x1fa, per
 * lib_otpinfo for XS3A. Same start, same bitmap encoding, same walk, so a
 * board provisioned with XMOS tooling reads back identically here -- a second
 * divergent reader would be a second thing to get wrong. */
#define OTP_BOARD_INFO_START 0x1fa

#define BITMAP_ABSENT     (1u << 31)   /* row never written */
#define BITMAP_VALID      (1u << 30)   /* a header lives here */
#define BITMAP_LEN_SHIFT  25u
#define BITMAP_LEN_MASK   0x1fu
#define BITMAP_MACS_SHIFT 22u
#define BITMAP_MACS_MASK  0x7u
#define BITMAP_SERIAL_BIT  21u
#define BITMAP_BOARDID_BIT 20u

/* ---- tile-pinned resource --------------------------------------------- */
/* File scope: XC only accepts `on tile[n]:` for file-scope resource
 * declarations, not inside a function body. Pinned to tile[0], where the OTP
 * data port lives on xcore.ai -- and the task that uses it is placed on
 * tile[0] in src/core/user_main.h so the placement matches. */
on tile[0]: OTPPorts csd_otp_ports = OTP_PORTS_INITIALIZER;

/* ---- primitives ------------------------------------------------------- */

/* Differential mode, as lib_otpinfo uses on XS3A: single-ended OTP reads on
 * xcore.ai are not dependable, and a silently-misread serial is exactly the
 * failure this module must not have. */
static unsigned otp_word(OTPPorts &ports, unsigned address)
{
    unsigned value = 0;
    otp_read_differential(ports, address, &value, 1);
    return value;
}

/* FNV-1a/64. A handful of instructions on xcore, no lookup table to
 * relocate, and the job is a fingerprint over 7 words -- not a cryptographic
 * commitment, which would cost cycles and code we do not have to spare.
 * Byte order is fixed big-endian so a host can recompute the hash from the
 * material words in csd_identity.h and get the same number. */
static unsigned long long fnv1a64_be(const unsigned w[], unsigned n)
{
    unsigned long long h = 0xcbf29ce484222325ull;
    for (unsigned i = 0; i < n; i++) {
        unsigned v = byterev(w[i]);                 /* fixed BE wire order */
        unsigned char *b = (unsigned char *)&v;
        for (unsigned j = 0; j < 4; j++) {
            h ^= (unsigned long long)b[j];
            h *= 0x100000001b3ull;
        }
    }
    return h;
}

/* Walk back from OTP_BOARD_INFO_START for a valid header. Returns 0 on a
 * blank or malformed region; the raw word is still handed back so the caller
 * can report the zeros, keeping "could not read" distinguishable from
 * "nothing provisioned". */
static int otp_find_header(OTPPorts &ports, unsigned *header_addr, unsigned *bitmap)
{
    int address = OTP_BOARD_INFO_START;
    for (;;) {
        unsigned bm = otp_word(ports, (unsigned)address);
        *header_addr = (unsigned)address;
        *bitmap = bm;

        if (bm & BITMAP_ABSENT) return 0;           /* never written */
        if (bm & BITMAP_VALID)  return 1;           /* header is here */

        /* Not a header: a payload word. Step back over it using the length
         * it carries. Zero length is malformed -- return rather than spin:
         * this walks an address derived from memory a mis-provisioned board
         * controls, so a runaway decrement is a real failure mode. */
        unsigned len = (bm >> BITMAP_LEN_SHIFT) & BITMAP_LEN_MASK;
        if (len == 0) return 0;
        address -= (int)len;
        if (address < 0) return 0;
    }
    /* Unreachable: every exit above is a return and the loop has no break.
     * Stated anyway so the analyzer stops reporting a fallthrough that would
     * otherwise leave the result undefined. */
    return 0;
}

/* Takes the ROUTING id, not a logical tile index.
 *
 * Observed with the literal 0/1: every read failed at once and the task never
 * advanced past stage 1, while the codec server on the same tile stayed
 * healthy. Per the lib_xs1 docs a register error should have returned 0 and
 * let the task continue, so a call that does not return points at a request
 * that never gets answered -- consistent with a packet addressed to a routing
 * id that names no tile. The mechanism is inference; the stage-1 stall is the
 * measurement.
 *
 * Callers supply get_tile_id(tile[n]), as XMOS code itself does (e.g. the
 * PLL/reset sequence in module_usb_audio's reboot path). The id is resolved by
 * the caller rather than here because `tile` is only usable as a file-scope
 * resource type, not as a function parameter type. */
static unsigned sswitch_read(unsigned route_id, unsigned reg, unsigned fail_sentinel)
{
    unsigned v = 0;
    if (!read_sswitch_reg(route_id, reg, v)) {
        /* Distinct non-zero per call site so "read failed" is never confused
         * with "read zero" -- both used to return 0, which made a dead read
         * path indistinguishable from a blank board. */
        return fail_sentinel;
    }
    return v;
}

/* Record progress ONLY.
 *
 * This used to be publish(m, stage), which called csd_identity_report() after
 * every read. Each partial pass therefore ran FNV-1a over whatever happened to
 * be filled in so far and published the result as a DV serial -- a well-formed,
 * plausible, WRONG unit identity at every intermediate stage. A fake serial in a
 * qualification record is worse than no serial, because it reads as data. What
 * the staged design actually needs is progress; the identity is reported exactly
 * once, after the last read. */
static void stage(unsigned s)
{
    csd_identity_stage(s);
}

/* ---- entry ------------------------------------------------------------ */
/* Runs as its own thread, placed on tile[0] in src/core/user_main.h: the OTP
 * data port is a tile[0] resource, and keeping the read out of
 * AudioHwRemoteTile0 is what stops a blocked OTP access from killing the
 * codec server and, through it, the whole diagnostic interface. */
void csd_identity_task(void)
{
    unsigned m[CSD_ID_MATERIAL_WORDS];
    for (unsigned i = 0; i < CSD_ID_MATERIAL_WORDS; i++) m[i] = 0;

    /* Publish BEFORE touching any hardware. Two reasons, and the second is
     * the reason this line must not be "cleaned up" into the final publish:
     *
     * 1. The serial's primary consumer is the USB descriptor, and
     *    enumeration does not wait for OTP. A host that reads iSerialNumber
     *    while a read is still in flight should get our pending marker, not
     *    lib_xua's factory constant.
     * 2. It is also the discriminator between the two remaining faults. The
     *    factory image answers iSerial "123456789" (SERIAL_STR). If this
     *    entry publish lands, the thread demonstrably runs and whatever is
     *    wrong lies in the reads that follow; if it does not land, the
     *    thread itself never started. Without it those two are
     *    indistinguishable -- and the stage counter cannot separate them
     *    either, because it is read over the tile-1 SCPI path.
     *
     * No printstr anywhere in this task: print* rides the xscope link and can
     * block when nothing drains it, a hazard this project already paid for on
     * an audio thread. Instrumenting a stall with a call that can itself stall
     * makes the instrument the suspect. */
    csd_identity_usb_publish();

    /* Stage 1: this tile's own ids. No port access, no cross-tile hop. If
     * only this much publishes, the failure is in stage 2 or 3.
     *
     * The two reads are published INDIVIDUALLY. Both sswitch reads failed on
     * the first hardware run, and doing them together left it undetermined
     * whether the first blocked, the second blocked, or neither ran at all --
     * three different faults with three different fixes. Publishing between
     * them makes the next run name the one that blocks. */
    /* stage(1) is the FIRST instruction that touches nothing. Without it,
     * STAGe? == 0 cannot distinguish "this thread was never scheduled" from
     * "it stalled inside the first read_sswitch_reg" -- and those are
     * different bugs with different fixes. The IPENDING marker cannot
     * separate them either: display_str() returns that marker whenever
     * s_ready is 0, so it reads the same whether the entry publish ran or
     * never got the chance. */
    stage(1u);

    /* Routing ids, resolved once. get_tile_id() is the only correct way to
     * name a tile to the switch: the logical index in tile[n] is NOT the
     * address the interconnect routes to. */
    const unsigned t0 = get_tile_id(tile[0]);
    const unsigned t1 = get_tile_id(tile[1]);

    m[0] = XS1_SS_NODE_ID_ID(sswitch_read(t0, XS1_SSWITCH_NODE_ID_NUM, 0xDEAD0001u));
    stage(2u);
    m[2] = sswitch_read(t0, XS1_SSWITCH_JTAG_DEVICE_ID_NUM, 0xDEAD0002u);
    stage(3u);

    /* Cross-tile sswitch access -- reading the OTHER tile's registers from a
     * tile[0] thread. That is the plausible suspect for the stall, which is
     * precisely why it gets its own stage rather than sharing one. */
    m[1] = XS1_SS_NODE_ID_ID(sswitch_read(t1, XS1_SSWITCH_NODE_ID_NUM, 0xDEAD0003u));
    stage(4u);
    m[3] = sswitch_read(t1, XS1_SSWITCH_JTAG_DEVICE_ID_NUM, 0xDEAD0004u);
    stage(5u);

    /* Stage 3: OTP board info. Blank on an unprovisioned board, which is the
     * expected state on the bench: the header word reads 0, which is neither
     * "absent" (bit31) nor "valid" (bit30), so the walk stops at its
     * invalid-length guard and the derived fingerprint stands. */
    unsigned addr = 0, bitmap = 0;
    if (otp_find_header(csd_otp_ports, &addr, &bitmap)) {
        unsigned nmacs = (bitmap >> BITMAP_MACS_SHIFT) & BITMAP_MACS_MASK;
        if (bitmap & (1u << BITMAP_SERIAL_BIT)) {
            m[5] = otp_word(csd_otp_ports, addr - (nmacs * 2u + 1u));
        }
        if (bitmap & (1u << BITMAP_BOARDID_BIT)) {
            unsigned skip = nmacs * 2u + ((bitmap >> BITMAP_SERIAL_BIT) & 1u);
            m[6] = otp_word(csd_otp_ports, addr - (skip + 1u));
        }
    }
    m[4] = bitmap;
    csd_identity_stage(6u);
    csd_identity_report(m[4], m[5], m[6], m[0], m[1], m[2], m[3],
                        fnv1a64_be(m, CSD_ID_MATERIAL_WORDS));

    /* The one and only descriptor update, after the value has stopped
     * changing. */
    csd_identity_usb_publish();
}
