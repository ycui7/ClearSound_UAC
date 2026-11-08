/* L/R trim persistence (TODO item 3, directive 2026-11-01).
 *
 * Stores the two-channel trim pair so it survives power cycles, in the DATA
 * PARTITION of the board's boot QSPI flash. OTP is OFF LIMITS by standing
 * owner directive: nothing here reads or writes OTP (csd_identity.xc owns
 * OTP reads, and it is read-only by rule).
 *
 * WHY THE DATA PARTITION AND NOT A HAND-PICKED ADDRESS
 * The first draft of this file addressed the flash by absolute address. That
 * is the dangerous shape: the boot ROM scans the boot partition for XEF
 * blocks, so a wrong address can brick a board with no way back except a
 * jig. XTC 15.3.0 offers a better API -- fl_readData() / fl_writeData() /
 * fl_eraseDataSector() address the DATA PARTITION BY OFFSET. The tools own
 * placement of that partition, so it cannot overlap the bootloader, the
 * factory image or an upgrade image, and the ROM never scans it for boot
 * blocks. Verified against target/include/quadflash.h in 15.3.0: the
 * absolute-address calls the first draft used (fl_read, fl_sectorErase,
 * fl_pageWrite, and the fl_QuadFlashSpec / fl_res types) DO NOT EXIST in
 * this toolchain -- it could not have linked.
 *
 * WHAT ERASES IT -- accepted by the owner 2026-11-01 ("ok to flash, add note
 * to todo about persistence data get wiped"), and recorded in TODO.md item 3:
 * the data partition is NOT preserved by reprogramming the boot partition.
 * It is rewritten by `xflash --data <file>`, by a full-chip --write-all, and
 * by any recipe that changes --boot-partition-size (which moves it). A plain
 * DFU firmware update writes boot images only, so trim SHOULD survive it --
 * rig/trim_persist_check.py makes that a measured leg, not an assumption.
 * Manufacturing therefore sets trim LAST, after the final firmware flash.
 * If the partition is absent (board flashed without --data) every call here
 * fails safe to 0 dB / 0 dB and reports it, rather than inventing a region.
 *
 * WHY A JOURNAL IN ONE SECTOR
 * The record is 32 bytes and rewritten only when a trim is set, but flash
 * gives no free write: a byte can only go 1->0, so an update needs an erase,
 * and an erase is the whole sector. Power-loss in the middle of an
 * erase-then-write is the only way this can corrupt, so the store never
 * overwrites in place: it APPENDS fixed 32-byte slots through the sector and
 * a load picks the newest VALID slot. A torn slot fails its checksum and the
 * previous complete slot wins -- the same reason a NOR filesystem journals
 * instead of patching. When the sector fills, erase and restart from slot 0.
 * The tools' own documentation for this API says data-partition writes are
 * "not fail-safe ... if the operation is interrupted ... the data in the page
 * or sector is undefined" -- which is exactly the case the journal covers,
 * so the journal is required by the documented contract, not defensive
 * decoration.
 *
 * VALIDITY, per the directive: "default both to 0 dB upon reset/power up, if
 * no valid persistence found" -- validity is magic + version + length +
 * checksum, never "the flash is not erased". An all-0xFF sector is the
 * erased state AND would parse as counts 0xFFFFFF, so an erased-only test is
 * not a validity test.
 *
 * THREADING -- reached only through the codec server's channel
 * Every fl_* call drives PORT_SQI_* through quadflashlib's synchronous port
 * I/O and the .xn declares those ports on tile[0], so they must run on
 * tile[0]. They are called from AudioHwRemoteTile0's select loop, via
 * AUDIOHW_CMD_TRIM_LOAD / _SAVE / _STAT -- the proxy csd_diag_regs.xc
 * already uses for the I2C bus, whose sole owner is that same task. The
 * first design was a dedicated task on tile[0]; xmap rejected it (tile[0]
 * is at 8/8 cores and 10/10 timers), and [[combinable]] did not rescue it
 * because the timer is still reserved. See the header for the measurements.
 *
 * csd_identity.xc's lesson -- a blocking resource call ahead of the codec
 * server's select loop once killed the whole bench -- is respected in a
 * different way than it was there: flash work runs only INSIDE a select
 * case, so the server is demonstrably serving before any flash call exists,
 * and no flash call is ever issued from task startup.
 *
 * fl_connect()/fl_disconnect() bracket EACH operation instead of being
 * hoisted out. Holding the QSPI ports + clock block permanently would sit on
 * top of lib_dfu's runtime writer (dfu_flashlib_user.c calls fl_connect with
 * its own port block) and the two must not overlap. The cost is a few ms on a
 * path that already costs tens.
 */
#include <stdint.h>
#include <stddef.h>           /* offsetof, for the slot layout asserts below */
#include "csd_trim_store.h"
/* deliberately NOT csd_gen.h: this file must not read or write the tile[1]
 * mailbox statics -- see csd_trim_journal_now / csd_trim_boot_load. */

#include <xs1.h>
#include <platform.h>         /* PORT_SQI_CS / _SCLK / _SIO from the .xn */
#include <quadflashlib.h>

#define TRIM_MAGIC    0x43535254u   /* 'TRS' + N: neither the XE magic nor
                                     * the XEF magic, so a record can never
                                     * be mistaken for a boot block by
                                     * anything that scans flash. */
/* v2 (owner case 14, 2026-11-05): the record also carries the idle-power
 * timeout, so one 32 B slot persists the whole settings pair set and there is
 * one checksum, one seq and one validity gate instead of two stores that can
 * disagree. v1 records fail the version gate and fall back to the documented
 * defaults (0 dB / 0 dB, 300 s), which is the specified behaviour for "no valid
 * persistence found" -- and there are no field units to lose. */
/* v3 (upsampling option): the word after chk now carries the upsampling
 * option in bit 0. It lives after chk, so the checksum is EXTENDED to cover it
 * for v3 records -- a setting that gates a hardware filter path must not be the
 * one field in the record that no checksum protects. v2 records keep the
 * original six-word checksum and are still verifiable byte for byte. */
#define TRIM_VERSION  3u
#define TRIM_SLOT_BYTES   32u       /* 8 words: hdr(4) + payload(3) + spare(1) */
#define TRIM_MAX_SLOTS    128u      /* 4 KiB sector / 32 */

/* Clock block handle: INJECTED by the tile[0] server, never hardcoded here.
 * An earlier revision put XS1_CLKBLK_4 directly in the port struct from C
 * and the board died at fl_connect with "Program received signal
 * ET_ILLEGAL_RESOURCE ... in configure_clock_src" (measured on hardware,
 * 2026-11-01). A clock block is an XC-claimed tile resource: xmap allocates
 * them from `clock` declarations, and a raw integer in a C struct is a
 * handle nobody owns. lib_dfu gets away with its define because XC hands it
 * a claimed handle; we must do the same. The server calls
 * csd_trim_set_clkblk() with (unsigned)c_trim_qspi -- declared in
 * clearsounddac_board.xc -- before every operation, so a missing claim shows
 * up as connect failure, never as an exception. */
static unsigned tr_clkblk = 0;
void csd_trim_set_clkblk(unsigned clk) { tr_clkblk = clk; }

/* Our own failure codes, above the quadflashlib range, so a code in
 * csd_trim_last_error() is unambiguous about which step failed. */
#define TRIM_ERR_NO_PARTITION  0x100u   /* board flashed without --data */
#define TRIM_ERR_SCRATCH_SMALL 0x101u
#define TRIM_ERR_BEYOND_PARTITION 0x102u  /* cursor past the partition end */
#define TRIM_ERR_PARTITION_SMALL  0x103u  /* < one 4 KiB sector: unusable */
#define TRIM_ERR_NO_CLKBLK        0x104u  /* server never injected a clock */
/* Own code, deliberately not a TRIM_* one: a rig reading a failure back must
 * be able to tell "the serial did not read" from "the trim journal is bad",
 * or an identity fault gets reported as a persistence fault. */
#define FLASH_ERR_DEGENERATE      0x105u  /* 4Bh returned all-00 or all-FF */
#define FLASH_ERR_UNSTABLE        0x106u  /* two reads disagreed: do not trust */
#define FLASH_ERR_DIVIDER         0x107u  /* SCLK divider could not be pinned,
                                           * so the sampling phase is unknown
                                           * and the answer may be a rotation
                                           * of the real serial. Not trustable:
                                           * see csd_flash_read_unique_serial. */

typedef struct {
    uint32_t magic;
    uint32_t version_len;           /* version << 16 | payload words (3) */
    int32_t  trim_l_88;             /* 8.8 dB, 0 .. -8192 */
    int32_t  trim_r_88;
    uint32_t seq;                   /* monotone; newest wins */
    int32_t  pw_timeout_s;          /* owner case 14: -1 never, >0 seconds.
                                     * 0 is never journalled (owner: 0 is
                                     * transient by spec), but the field can
                                     * hold it without breaking the gate. */
    uint32_t chk;                   /* sum of the six words above it */
    uint32_t opt_bits;              /* v3+: bit0 = upsampling option. Covered by
                                     * slot_checksum() for v3 records; v2 wrote 0
                                     * here and is not covered, exactly as before. */
} trim_slot_t;                        /* 32 bytes */

/* The checksum walks this struct as raw words and, for v3, indexes word 7
 * directly. Any padding or member drift would silently change which bytes are
 * protected -- a calibration store that quietly stops checking itself is the
 * worst kind of passing build. Pin the layout at compile time. */
_Static_assert(sizeof(trim_slot_t) == TRIM_SLOT_BYTES,
               "trim slot layout drifted from the on-flash size");
_Static_assert(offsetof(trim_slot_t, opt_bits) == 28u,
               "option word moved off its checksummed index");

static uint32_t slot_checksum(const trim_slot_t *s)
{
    const uint32_t *w = (const uint32_t *)s;
    uint32_t c = 0;
    /* Six words, not five: the timeout sits before chk, so a bit flip in it
     * invalidates the record exactly like a flip in a trim. Nothing in this
     * store is allowed to be trusted unchecked -- that is how a torn write
     * becomes a silent wrong volume. */
    for (unsigned i = 0; i < 6u; i++) c += w[i];
    /* v3 moved the option word into the protected set. Older records are
     * checksummed exactly as their writer computed it, so migrating a record
     * never turns a valid v2 entry into a rejected one. */
    if ((s->version_len >> 16) >= 3u) c += w[7];
    return c;
}

/* ---- record compatibility -------------------------------------------------
 * A stored record carries its own type version in version_len[31:16]. Three
 * outcomes, and the difference matters:
 *
 *   CURRENT      version == TRIM_VERSION, all range gates pass          -> adopt
 *   MIGRATED     an older version whose layout still parses            -> adopt,
 *                fill the fields it predates with their defaults, and rewrite the
 *                record at the current version so the migration happens once
 *   INCOMPATIBLE version newer than this firmware, or unknown          -> do NOT
 *                adopt; a newer record may mean a different payload entirely, and
 *                guessing would install garbage. Counted, and the caller resets
 *   CORRUPT      magic or checksum wrong, or a payload word out of range/off-grid
 *                                                                       -> skip,
 *                and use the default condition rather than believing it
 *
 * A record that fails its checksum NEVER becomes the volume path's truth: the
 * store falls back to defaults (trims 0 dB, timeout 300 s) exactly as a blank
 * partition does. Silence in flash is not evidence of a good record.
 */
#define TRIM_VERSION_V1   1u        /* trims only; pw_timeout_s did not exist */
#define TRIM_VERSION_V2   2u        /* trims + idle timeout; no option word    */
#define TRIM_OPT_UPS      0x1u      /* opt_bits bit0: XMOS 8x + DAC FIR bypass */
#define TRIM_PW_DEFAULT_S 300

typedef enum {
    TRIM_SLOT_CORRUPT      = 0,
    TRIM_SLOT_CURRENT      = 1,
    TRIM_SLOT_MIGRATED     = 2,
    TRIM_SLOT_INCOMPATIBLE = 3
} trim_slot_class_t;

static volatile unsigned tr_st_crc_fail  = 0;   /* records rejected by checksum */
static volatile unsigned tr_st_unsupported = 0; /* version newer/unknown        */
static volatile unsigned tr_st_migrated  = 0;   /* records upgraded on read     */
static volatile unsigned tr_st_migrated_scan = 0; /* counted by the sector scan  */
static volatile unsigned tr_st_defaulted = 0;   /* NO valid record -> defaults  */

static trim_slot_class_t slot_classify(const trim_slot_t *s)
{
    if (s->magic != TRIM_MAGIC) return TRIM_SLOT_CORRUPT;

    unsigned ver = s->version_len >> 16;
    unsigned words = s->version_len & 0xFFFFu;

    /* Checksum before anything is trusted. A torn NOR write, a partial append
     * and a bit flip all land here, and all of them mean "do not adopt". */
    if (s->chk != slot_checksum(s)) return TRIM_SLOT_CORRUPT;

    if (ver > TRIM_VERSION) return TRIM_SLOT_INCOMPATIBLE;   /* newer firmware */

    if (ver == TRIM_VERSION) {
        if (words != 3u) return TRIM_SLOT_CORRUPT;
        if (s->pw_timeout_s < -1 || s->pw_timeout_s > 86400) return TRIM_SLOT_CORRUPT;
        if (s->trim_l_88 > 0 || s->trim_l_88 < -8192 || (s->trim_l_88 % 32)) return TRIM_SLOT_CORRUPT;
        if (s->trim_r_88 > 0 || s->trim_r_88 < -8192 || (s->trim_r_88 % 32)) return TRIM_SLOT_CORRUPT;
        /* Only bit0 is defined. Refuse the record rather than invent a meaning
         * for bits nobody wrote: garbage in a gate word is how a corrupt record
         * becomes boot behaviour. */
        if (s->opt_bits & ~TRIM_OPT_UPS) return TRIM_SLOT_CORRUPT;
        return TRIM_SLOT_CURRENT;
    }

    if (ver == TRIM_VERSION_V2) {
        /* The immediately preceding layout: trims and timeout, no option word.
         * Its option word is whatever earlier firmware wrote there, so it is
         * replaced by the default (option off), never believed. */
        if (words != 3u) return TRIM_SLOT_CORRUPT;
        if (s->pw_timeout_s < -1 || s->pw_timeout_s > 86400) return TRIM_SLOT_CORRUPT;
        if (s->trim_l_88 > 0 || s->trim_l_88 < -8192 || (s->trim_l_88 % 32)) return TRIM_SLOT_CORRUPT;
        if (s->trim_r_88 > 0 || s->trim_r_88 < -8192 || (s->trim_r_88 % 32)) return TRIM_SLOT_CORRUPT;
        return TRIM_SLOT_MIGRATED;
    }

    if (ver == TRIM_VERSION_V1) {
        /* The one older layout that is understood: trims in the same position,
         * no timeout word. Its timeout field is whatever the erased flash left
         * there, so it is NOT range-gated -- it is replaced, not believed. */
        if (s->trim_l_88 > 0 || s->trim_l_88 < -8192 || (s->trim_l_88 % 32)) return TRIM_SLOT_CORRUPT;
        if (s->trim_r_88 > 0 || s->trim_r_88 < -8192 || (s->trim_r_88 % 32)) return TRIM_SLOT_CORRUPT;
        return TRIM_SLOT_MIGRATED;
    }

    return TRIM_SLOT_INCOMPATIBLE;      /* unknown older version: same rule */
}

static int slot_valid(const trim_slot_t *s)
{
    trim_slot_class_t k = slot_classify(s);
    return k == TRIM_SLOT_CURRENT || k == TRIM_SLOT_MIGRATED;
}

/* (the pre-migration validator is gone: slot_classify() above is the one gate,
 * so a record cannot be accepted by one path and rejected by another) */

/* ---- status, read over SCPI by the rig ----------------------------------
 * Every failure in this file is silent by nature (a failed write simply
 * means the trim does not persist), so each one is latched for inspection.
 * VOLume:TRIM? reports these; without them a rig cannot tell "persisted"
 * from "never even tried". */
static volatile unsigned tr_st_partition = 0;   /* bytes; 0 = none present */
static volatile unsigned tr_st_loaded    = 0;   /* 0 pend,1 ok,2 no-rec,3 err */
static volatile unsigned tr_st_saved     = 0;   /* 0 never, 1 ok, 2 err */
static volatile unsigned tr_st_seq       = 0;   /* newest seq seen/written */
static volatile unsigned tr_st_err       = 0;   /* last failure code */
static volatile unsigned tr_st_load_n    = 0;   /* boot loads completed */
static volatile unsigned tr_st_save_n    = 0;   /* successful saves */

/* Idle-power timeout carried in the SAME record (owner case 14, so one
 * checksum and one seq cover both settings and they can never disagree).
 * The owner's 300 s default lives here until a valid record replaces it; the
 * power module keeps its own compile-time default and is told this value at
 * boot, so neither module has to import the other's constants. */
static volatile int32_t tr_pw_timeout_s = 300;

/* The trim pair that tile[0] most recently saw, published for the ONE writer of
 * the codec's channel-volume registers (vol_apply_digital in board.xc). It is
 * the value this store journalled or adopted -- never read from the tile[1]
 * mailbox, which is exactly what a tile[0] must not do (that bug journalled
 * zeros while seq climbed). Updated at every journal and every adopt, so it can
 * never disagree with what the record says. */
static volatile unsigned tr_applied_packed = 0u;

unsigned csd_trim_applied_packed(void) { return tr_applied_packed; }

void csd_trim_pw_timeout_set(int sec)
{
    /* Normalised here, once: the owner's spec says any negative means "never",
     * so -5000 is stored as the canonical -1 rather than as a number that
     * get() would later have to explain. The 24 h ceiling turns an absurd value
     * into a visible clamp instead of a box that never sleeps for a year. */
    if (sec < -1)   sec = -1;
    if (sec > 86400) sec = 86400;
    tr_pw_timeout_s = (int32_t)sec;
}

int csd_trim_pw_timeout_get(void) { return (int)tr_pw_timeout_s; }

/* Upsampling option, persisted alongside the trims so it survives power-up and
 * cannot disagree with them. DEFAULT 0 (off) is both the initialiser here and
 * the migration default for any record that predates the field -- the option
 * is never adopted from erased flash.
 *
 * set() stores only; the caller journals, exactly as the timeout does, so a
 * rate-change re-grant never appends a record. */
static volatile unsigned tr_ups_en = 0u;

void csd_trim_ups_set(unsigned on) { tr_ups_en = on ? 1u : 0u; }
unsigned csd_trim_ups_get(void)    { return tr_ups_en; }

/* No per-field accessors on purpose: these statics are read by the tile[0]
 * server and crossed to tile[1] one word at a time through
 * csd_trim_status_word() below, which is the scalar-only rule this codebase
 * follows at every tile boundary (csd_identity.h). Seven exported getters
 * would be seven ways to get that rule wrong. */

/* No request queue and no poll timer: the tile[1] diagnostic core asks for a
 * journal synchronously, over the codec server's existing channel (see the
 * header). A burst of trim sets therefore journals one at a time, in order,
 * and each VOLume:TRIM:* returns only once its own value is in flash --
 * which is what lets a rig read VOLume:TRIM:STATus? immediately after a set
 * and believe it, instead of racing a poll interval. */

/* ---- the flash side, reached only from the tile[0] task ----------------- */
/* clkblk 0 until csd_trim_set_clkblk() supplies the claimed handle. */
static fl_QSPIPorts tr_ports = { PORT_SQI_CS, PORT_SQI_SCLK, PORT_SQI_SIO, 0 };
static unsigned tr_next_slot;       /* append cursor, set by the load */

/* fl_connect per operation: see the threading note above. *res carries the
 * failing code (ours or quadflashlib's) on failure. */
/* Scratch is the page-cache quadflashlib requires for a partial-sector
 * write; fl_getWriteScratchSize() answers how much, and the tools require at
 * least a sector's worth. Static on purpose: an allocation that failed at
 * erase time would be the worst possible moment to discover it. */
#define TRIM_SECTOR_BYTES 4096u
static unsigned char tr_scratch[TRIM_SECTOR_BYTES];

/* Every call below addresses sector 0, which IS the partition (one sector),
 * so offset 0 is the partition start. fl_connect brackets each operation --
 * see the threading note at the top of the file -- and fl_disconnect runs on
 * EVERY exit path of every function here, including the error paths. That
 * matters: a leaked connect would strand the SQI ports and the clock block
 * on top of lib_dfu's own runtime writer.
 *
 * *res carries the failing code on failure -- ours (TRIM_ERR_*) or
 * quadflashlib's. psz/need may be 0 on failure: only the "did it work"
 * branch may use them. */
static int tr_partition_open(unsigned *res, unsigned *psz, unsigned *need)
{
    /* No claimed clock: refuse before touching the ports. An unclaimed
     * handle does not return an error, it raises a RESOURCE EXCEPTION that
     * kills the codec server -- the one failure mode this file must never
     * produce, because that task is the sole owner of the codec, mute and
     * volume path. */
    if (tr_clkblk == 0u) { *res = TRIM_ERR_NO_CLKBLK; return 0; }
    tr_ports.qspiClkblk = tr_clkblk;

    int r = fl_connect(&tr_ports);
    if (r) { *res = (unsigned)r; fl_disconnect(); return 0; }

    unsigned sz = fl_getDataPartitionSize();
    if (sz == 0u) {                     /* board flashed without --data */
        tr_st_partition = 0u;
        fl_disconnect();
        *res = TRIM_ERR_NO_PARTITION;
        return 0;
    }
    tr_st_partition = sz;
    *psz = sz;

    /* The partition must be at least one whole sector. The rewrite path
     * erases a sector and writes a sector regardless (erase is sector-
     * granular, so it reaches past a smaller partition), and tr_scan indexes
     * 128 slots. A partition smaller than a sector is therefore not usable
     * here at all -- and continuing would put an ERASE outside the region the
     * tools assigned us, which is the one failure this design cannot take
     * back. Refuse with a code the rig can see. (Seed the journal with a full
     * 4 KiB file: see flash_persist.sh.) */
    if (sz < TRIM_SECTOR_BYTES) {
        fl_disconnect();
        *res = TRIM_ERR_PARTITION_SMALL;
        return 0;
    }

    unsigned n = fl_getWriteScratchSize(0u, TRIM_SECTOR_BYTES);
    if (n > sizeof tr_scratch) {        /* our static is too small to be safe */
        fl_disconnect();
        *res = TRIM_ERR_SCRATCH_SMALL;
        return 0;
    }
    *need = n;
    return 1;
}

/* ---- factory unique serial (identity root, NOT trim persistence) ---------
 * 4Bh "Read Unique ID Number" on the Winbond W25Q16JV: a factory-set,
 * read-only 64-bit number, unique per device. Measured on board 3:
 * 0x443258C251377B1C, identical across 6 reads in 2 boots and 3 cold image
 * loads, so it is a genuine per-unit root -- unlike the derived fingerprint,
 * whose four sswitch words are board-configuration constants and therefore
 * the SAME on every board of this design (all three boards report 201b0d69).
 *
 * This lives here rather than in csd_identity.xc because this file owns the
 * QSPI access: quadflashlib is a SINGLETON (no device handle on any entry
 * point) and csd_diag.h:90 makes the codec server the only task allowed to
 * drive PORT_SQI_*. An identity-thread read would put a second, unordered
 * fl_connect beside the trim journal's and lib_dfu's runtime writer, with no
 * lock anywhere to keep them apart. Same reason the trim calls ride this
 * file: the ports have exactly one owner.
 *
 * Deliberately does NOT go through tr_partition_open: the serial lives in the
 * die, not in the data partition, so the read must still work on a board
 * flashed without --data. It brackets fl_connect/fl_disconnect itself, like
 * every other operation here.
 *
 * READ-ONLY: the only opcode issued is 0x4B. Audit with
 *   grep -nE "0x(06|04|02|20|52|D8|C7|60|01|42|44|31|11|BE|99|66|B9)" csd_trim_store.c
 * -- any hit outside a comment is a defect. There is no WREN, no erase, no
 * program and no write-status-register anywhere in this function. */
#define FLASH_OP_READ_UNIQUE_ID   0x4Bu
#define FLASH_UNIQUE_ID_DUMMY     4u          /* datasheet: four dummy bytes */
#define FLASH_UNIQUE_ID_BYTES     8u          /* then 8 bytes out on DO      */

static unsigned s_uid_hi, s_uid_lo, s_uid_err;

unsigned csd_flash_serial_hi(void)  { return s_uid_hi; }
unsigned csd_flash_serial_lo(void)  { return s_uid_lo; }
unsigned csd_flash_serial_err(void) { return s_uid_err; }

/* Returns 1 on a usable serial, 0 with the reason left in s_uid_err. No
 * pointer parameters anywhere: this file is C and the only caller is XC
 * (clearsounddac_board.xc), and a C-defined `unsigned*` argument called from
 * XC does not link -- see csd_identity.h's note on the same boundary. Scalar
 * getters instead, the shape csd_trim_status_word() already uses. */
/* The sampling phase of a 4Bh read is set by the SCLK DIVIDER. Measured in one
 * binary, one fl_connect, one documented variable (probe_div2.xc):
 *
 *     no override (fl_connect's own setup)  0x443258C251377B1C  phase 0
 *     fl_dividerOverride(2)                 rotation by 39
 *     fl_dividerOverride(3)                 0x443258C251377B1C  phase 0
 *     fl_dividerOverride(4)                 rotation by 13
 *     fl_dividerOverride(5..8)              rotation by 19
 *
 * Every wrong value is a pure ROTATION of the same 64-bit cycle, which is why
 * none of them looked corrupt: 4Bh streams the ID cyclically, so a phase error
 * moves the sampling window and changes nothing else. A rotated serial is
 * stable, non-degenerate and entirely plausible -- the dangerous kind of wrong.
 *
 * Hence the divider is PINS to 3 here rather than inherited. Two reasons it
 * must be explicit:
 *   - The board boots from this die, so the QSPI block has already been driven
 *     by the boot loader at whatever divider it chose for quad-read. Whatever
 *     fl_connect leaves behind is therefore a property of the boot path, not
 *     of this call. A serial that follows the boot loader's divider is a serial
 *     that changes when someone passes --quad-spi-clock, which is a build flag.
 *   - div 3 is the toolchain default (xflash --spi-div defaults to 3) and is
 *     the divider the standalone probe used when it read the datasheet value,
 *     so pinning it costs no speed relative to the known-good configuration.
 * The part is rated 100 MHz and the .xn declares ReferenceFrequency=100MHz, so
 * div 1 would put SCLK exactly on the ceiling -- far too close for a command
 * whose dummy-bit count is not documented per opcode in this driver.
 *
 * The override LATCHES: the return-control row above stayed at rotation 19
 * after the sweep rather than returning to phase 0. So this is also why an
 * earlier fl_qspiInit sweep was worthless -- re-initialising the QSPI block
 * destroys the phase fl_connect established, and every row after the first
 * measured that side effect instead of the variable it claimed to test. */
#define FLASH_SPI_DIV 3           /* the one divider measured to sample 4Bh at
                                   * phase 0; see the block above */

static unsigned char s_uid_dummy[FLASH_UNIQUE_ID_DUMMY];
static unsigned char s_uid_out[FLASH_UNIQUE_ID_BYTES];
static unsigned char s_uid_out2[FLASH_UNIQUE_ID_BYTES];

/* Returns 1 on a usable serial, 0 with the reason left in s_uid_err. No
 * pointer parameters anywhere: this file is C and the only caller is XC
 * (clearsounddac_board.xc), and a C-defined `unsigned*` argument called from
 * XC does not link -- see csd_identity.h's note on the same boundary. Scalar
 * getters instead, the shape csd_trim_status_word() already uses. */
int csd_flash_read_unique_serial(void)
{
    s_uid_hi = s_uid_lo = s_uid_err = 0u;

    for (unsigned i = 0; i < FLASH_UNIQUE_ID_DUMMY; i++) s_uid_dummy[i] = 0u;
    for (unsigned i = 0; i < FLASH_UNIQUE_ID_BYTES; i++) {
        s_uid_out[i]  = 0u;
        s_uid_out2[i] = 0u;
    }

    /* Same guard as tr_partition_open: an unclaimed clock handle does not
     * return an error, it raises a RESOURCE EXCEPTION, and from the codec
     * server that kills mute and volume along with everything else. */
    if (tr_clkblk == 0u) { s_uid_err = TRIM_ERR_NO_CLKBLK; return 0; }
    tr_ports.qspiClkblk = tr_clkblk;

    int r = fl_connect(&tr_ports);
    if (r) { s_uid_err = (unsigned)r; fl_disconnect(); return 0; }

    /* Pin the SCLK divider for determinism, NOT as the phase fix.
     *
     * fl_dividerOverride is documented as "must be called after fl_connect".
     * In a STANDALONE image the divider genuinely sets the sampling phase
     * (div 3 -> datasheet order, div 2/4/5 -> rotations of it), so pinning it
     * removes one real degree of freedom and div 3 is the value the known-good
     * readings used. Measured in THIS firmware it changed nothing -- the phase
     * came back rotated either way -- because the board boots from this die,
     * so the boot loader has already configured the block driving SCLK for
     * 100 MHz quad read before any of our code runs. So do not read this call
     * as a fix: the guarantee comes from canonicalising below, not from here.
     *
     * A non-zero return is treated as fatal regardless: it means the divider
     * could not be set, which leaves the phase dependent on whatever the boot
     * path did, and this read is only trustworthy as a per-unit constant. */
    r = fl_dividerOverride(FLASH_SPI_DIV);
    if (r) { s_uid_err = FLASH_ERR_DIVIDER; fl_disconnect(); return 0; }

    /* cmd is the bare opcode: bits 12..15 (input lanes) and 8..11 (output
     * lanes) are left 0, which the library documents as its 1-lane default.
     * That is the 1-1-1 form the datasheet specifies for 4Bh. */
    r = fl_command(FLASH_OP_READ_UNIQUE_ID, s_uid_dummy, FLASH_UNIQUE_ID_DUMMY,
                   s_uid_out, FLASH_UNIQUE_ID_BYTES);
    s_uid_err = (unsigned)r;
    if (r) { fl_disconnect(); return 0; }     /* disconnect on EVERY exit */

    /* A second read must agree with the first. WHAT THIS PROVES: that the
     * driver sampled the same ring twice, i.e. the answer is a register and
     * not bus noise. WHAT IT DOES NOT PROVE: that the phase is the one the
     * datasheet would print -- canonicalisation below makes that irrelevant,
     * because every rotation of this ID maps to one value.
     *
     * Keep it anyway: it is the only thing standing between a marginal
     * transfer and a serial that quietly changes. Two reads on one connect
     * cost 9 bytes of wire and one compare, and if the phase ever moved
     * mid-boot the two reads would disagree and the board degrades to DERIVED
     * rather than re-serialising -- which would make the host enumerate a new
     * device. Note both reads share this connect, so it is insensitive to a
     * phase that is stable within a boot but differs between boots; the
     * canonical form is what makes that case harmless too. */
    int r2 = fl_command(FLASH_OP_READ_UNIQUE_ID, s_uid_dummy, FLASH_UNIQUE_ID_DUMMY,
                        s_uid_out2, FLASH_UNIQUE_ID_BYTES);
    fl_disconnect();
    if (r2) { s_uid_err = (unsigned)r2; return 0; }
    for (unsigned i = 0; i < FLASH_UNIQUE_ID_BYTES; i++) {
        if (s_uid_out2[i] != s_uid_out[i]) {
            s_uid_err = FLASH_ERR_UNSTABLE; return 0;
        }
    }

    /* A degenerate answer means the read did not happen, not that the part's
     * serial is blank -- the datasheet has no blank-serial case. Hashing
     * 0x0000000000000000 or 0xFFFFFFFFFFFFFFFF would hand every such board
     * the same serial and look exactly like a working identity. Refusing is
     * the only honest result, and it is loud: see the caller. */
    int nz = 0, nff = 0;
    for (unsigned i = 0; i < FLASH_UNIQUE_ID_BYTES; i++) {
        if (s_uid_out[i] != 0x00u) nz++;
        if (s_uid_out[i] != 0xFFu) nff++;
    }
    if (nz == 0 || nff == 0) { s_uid_err = FLASH_ERR_DEGENERATE; return 0; }

    /* Wire order is big-endian (s_uid_out[0] is the MSB). */
    unsigned long long v = 0;
    for (unsigned i = 0; i < FLASH_UNIQUE_ID_BYTES; i++)
        v = (v << 8) | (unsigned long long)s_uid_out[i];

    /* CANONICALISE against the sampling phase.
     *
     * 4Bh streams the 64-bit ID CYCLICALLY -- one capture returned 24 bytes
     * with the same 8 bytes three times over. So the wire is a ring and a
     * "wrong" read is not corruption: it is the same ring sampled at a
     * different bit offset. Every value ever measured here -- the standalone
     * probe's, the firmware's, and the in=3/in=5 and divider-sweep rows -- is
     * a pure rotation of one cycle, which is why not one of them looked wrong:
     * a rotated serial is stable, non-degenerate and entirely plausible.
     *
     * WHY this is the fix rather than pinning the phase: the phase is set by
     * the SCLK divider on whatever clock block drives SCLK, and the app cannot
     * choose it. Measured: fl_dividerOverride(3) moves the phase in a
     * standalone image (div 3 -> phase 0, div 2/4/5 -> rotations) and does
     * NOTHING in this firmware -- because the board boots from this very die,
     * so the boot loader has already configured that block for 100 MHz quad
     * read before any of our code runs. Confirmed by two binaries issuing an
     * identical fl_command from identical source lines and getting different
     * phases, each deterministic over 3 cold boots, while fl_command itself is
     * a fixed ELF object in libquadflash.a the optimiser cannot rewrite.
     *
     * Taking the maximum over all 64 rotations is the standard way to read a
     * cyclic identifier: it is a class invariant, so the result is identical
     * whichever phase the driver happened to sample at, and it cannot collide
     * across distinct IDs -- two different cycles have different maxima,
     * verified over 200k random 64-bit values with zero collisions. A 64x64
     * bit-scan runs once at boot; the part has no FPU and this is not a frame
     * path, so the cost is irrelevant.
     *
     * Consequence for anyone reading a label: the value stored here is the
     * CANONICAL rotation, not the datasheet's byte order. On this die both
     * 0x443258C251377B1C (datasheet order) and 0x22192C61289BBD8E (what an
     * unphased read gives) canonicalise to 0xF6388864B184A26E. */
    {
        unsigned long long best = v;
        for (unsigned k = 1; k < 64u; k++) {
            unsigned long long rot = (v >> k) | (v << (64u - k));
            if (rot > best) best = rot;
        }
        v = best;
    }
    s_uid_hi = (unsigned)(v >> 32);
    s_uid_lo = (unsigned)(v & 0xFFFFFFFFu);
    return 1;
}

static int tr_read_sector(unsigned char *sec, unsigned len, unsigned *res)
{
    unsigned psz = 0, need = 0;
    if (!tr_partition_open(res, &psz, &need)) return 0;
    if (len > psz) len = psz;
    /* Preset before the capped read. If the partition is smaller than the
     * buffer, the tail would otherwise hold stale bytes from a previous call
     * and tr_scan would parse them as slots -- stale data being read as a
     * record is exactly the failure this store must not have. 0xFF is also
     * the honest value: past the partition there is nothing of ours. */
    for (unsigned i = len; i < TRIM_SECTOR_BYTES; i++) sec[i] = 0xFFu;
    int r = fl_readData(0u, len, sec);
    *res = (unsigned)r;
    fl_disconnect();
    return (r == 0);
}

/* APPEND one slot. Deliberately does NOT erase: NOR programming turns 1s
 * into 0s, so writing a 32-byte slot into an erased region needs no erase,
 * and fl_writeData only needs the scratch page-cache to preserve the REST of
 * a sector -- there is no other data here to preserve. This is the whole
 * point of the journal design: the destructive erase happens once per 128
 * writes (at wrap) instead of once per write, so the power-loss window this
 * code can lose data in is 128x rarer than an erase-then-write-on-every-save
 * design. */
static int tr_append_slot(unsigned slot, const unsigned char *sec, unsigned *res)
{
    unsigned psz = 0, need = 0;
    if (!tr_partition_open(res, &psz, &need)) return 0;
    unsigned off = slot * TRIM_SLOT_BYTES;

    /* Hard bound. The cursor came from tr_scan over a buffer that was capped
     * to psz, so on a full 4 KiB partition this cannot trigger -- but it is
     * the ONE place this file could land outside the tool-owned partition,
     * and outside the partition is the only place it could damage something
     * that is not our own record. Fail loudly rather than find out. */
    if (off + TRIM_SLOT_BYTES > psz) {
        fl_disconnect();
        *res = TRIM_ERR_BEYOND_PARTITION;
        return 0;
    }

    int r = fl_writeData(off, TRIM_SLOT_BYTES, sec + off, tr_scratch);
    *res = (unsigned)r;
    fl_disconnect();
    return (r == 0);
}

/* Full rewrite, erase included -- used ONLY when the journal has filled and
 * is restarting at slot 0. The single destructive path in this file. */
static int tr_rewrite_sector(const unsigned char *sec, unsigned *res)
{
    unsigned psz = 0, need = 0;
    if (!tr_partition_open(res, &psz, &need)) return 0;
    int r = fl_eraseDataSector(0u);
    if (r == 0) r = fl_writeData(0u, sizeof tr_scratch, sec, tr_scratch);
    *res = (unsigned)r;
    fl_disconnect();
    return (r == 0);
}

/* Newest valid slot + the next free slot index. Returns the winning seq. */
static unsigned tr_scan(const unsigned char *sec, unsigned len,
                        const trim_slot_t **best, unsigned *fill)
{
    const trim_slot_t *b = 0;
    uint32_t bseq = 0;
    unsigned f = 0;
    unsigned nslots = len / TRIM_SLOT_BYTES;
    if (nslots > TRIM_MAX_SLOTS) nslots = TRIM_MAX_SLOTS;
    for (unsigned i = 0; i < nslots; i++) {
        const trim_slot_t *s = (const trim_slot_t *)(sec + i * TRIM_SLOT_BYTES);
        if (*(const uint32_t *)s == 0xFFFFFFFFu) { f = i; break; }   /* first erased */
        /* Classify once and act on the class: torn slots are skipped and never
         * reused (NOR cannot restore a 1 bit, so a partially-written slot stays
         * dirty), an unusable version is counted so the caller can reset, and
         * only CURRENT/MIGRATED records compete for "newest". */
        trim_slot_class_t k = slot_classify(s);
        if (k == TRIM_SLOT_MIGRATED) tr_st_migrated_scan++;
        else if (k == TRIM_SLOT_INCOMPATIBLE) tr_st_unsupported++;
        else if (k == TRIM_SLOT_CORRUPT) tr_st_crc_fail++;
        if (k == TRIM_SLOT_CURRENT || k == TRIM_SLOT_MIGRATED) {
            if (!b || (int32_t)(s->seq - bseq) > 0) { /* wraps correctly */
                b = s; bseq = s->seq;
            }
        }
        f = i + 1u;                               /* no erased slot yet: full */
    }
    *best = b; *fill = f;
    return bseq;
}

/* Adopt the persisted pair. Called by the diagnostic task at startup through
 * the proxy, and re-runnable on demand (VOLume:TRIM:LOAD) so a rig can
 * re-read flash without restarting the firmware.
 *
 * Returns the pair to the CALLER and does not publish it. The mixer reads
 * only csd_mb_trim, a tile[1]-local static, and this file runs on tile[0]:
 * an earlier revision had the store call csd_mb_put_trim() itself, which
 * wrote tile[0]'s private copy, so flash verified good (partition=1048576,
 * load=1, seq=4, err=0) while the DAC stayed at 0 dB and the mic measured
 * -0.95 dB instead of -8 dB. tile[1] applies it -- see csd_diag_midi.xc --
 * so the mixer still has exactly one writer and one route into the multiply.
 *
 * Boot ordering is safe by construction rather than by luck: the mixer only
 * ever multiplies HOST audio, which cannot arrive until USB has enumerated
 * -- host-side seconds after this load completes. A host cannot hear the
 * difference between "0 dB" and "not loaded yet". */
unsigned csd_trim_boot_load(void)
{
    static unsigned char sector[4096];
    unsigned res = 0;
    unsigned packed = 0u;                         /* documented reset default */

    unsigned len = sizeof sector;
    if (tr_read_sector(sector, len, &res)) {
        const trim_slot_t *best = 0;
        unsigned fill = 0;
        unsigned seq = tr_scan(sector, len, &best, &fill);
        tr_next_slot = fill;
        tr_st_load_n++;
        if (best) {
            packed = (((unsigned)(uint32_t)best->trim_r_88) << 16)
                        |  ((unsigned)(uint32_t)best->trim_l_88 & 0xFFFFu);
            tr_st_seq    = seq;
            tr_st_loaded = 1u;
            /* Adopt the persisted timeout only from a record that passed every
             * gate (magic, version, payload count, checksum, range) -- an
             * unvalidated adopt is how a corrupt word becomes boot behaviour. */
            /* Forward migration happens HERE, at adoption: an older record's
             * absent fields take their defaults instead of whatever the erased
             * flash left in that word. Counted, so a rig can see that a
             * migration took place rather than inferring it from a value. */
            /* Field-by-field, by the record's OWN version: default only the
             * fields a record predates, never the ones it legitimately carries.
             * A blanket "older -> defaults" would discard an operator's stored
             * idle timeout on the v2 -> v3 step, which is exactly the data loss
             * the migration path exists to avoid. The timeout became a real
             * field at v2; the option word only at v3. */
            {
                const unsigned bver = best->version_len >> 16;
                unsigned defaulted_something = 0u;
                if (bver >= 2u) {
                    tr_pw_timeout_s = best->pw_timeout_s;
                } else {
                    tr_pw_timeout_s = TRIM_PW_DEFAULT_S;
                    defaulted_something = 1u;
                }
                if (bver >= 3u) {
                    tr_ups_en = best->opt_bits & TRIM_OPT_UPS;
                } else {
                    tr_ups_en = 0u;   /* off, never the erased-flash word */
                    defaulted_something = 1u;
                }
                if (defaulted_something) tr_st_migrated++;
            }
            tr_applied_packed = (unsigned)(((unsigned short)best->trim_r_88) << 16)
                              | (unsigned short)best->trim_l_88;
        } else {
            tr_st_loaded = 2u;                    /* connected, nothing valid */
        }
    } else {
        tr_st_loaded = 3u;
        tr_st_err    = res;
    }
    /* NO POINTER. The rule is scalars across a tile boundary, and it is also
     * the only shape that links: a C-defined function with an unsigned*
     * parameter, called from XC, fails xlink with "Type of symbol has
     * mismatch, found ... C pointer, previous ... xC pointer". The compiler
     * suggests unsafe on the pointer, which belongs on the declaration -- and
     * a C header cannot spell that. Returning the value removes the pointer
     * and the rule violation together. Load status is already a scalar
     * (csd_trim_status_word index 1), so the server still replies both words.
     * Caller (tile[1]) applies the returned pair to its OWN mailbox static.
     * (Careful quoting: this file once failed to build because a comment
     * quoted the compiler's own slash-star / star-slash tokens and ended the
     * block comment early. Do not paste comment delimiters into prose.) */
    return packed;
}

/* Journal the given pair. The value is an ARGUMENT, never read from the
 * mailbox: this runs on tile[0] and the mailbox static is tile[1] local, so
 * reading it here journalled zeros while seq happily climbed. Returns 1 on
 * success and 0 on any flash failure; the failure code is latched either way,
 * so the caller's reply is honest. */
/* Reset the store to the default condition: 0 dB trims, 300 s timeout.
 *
 * This is the escape hatch for the bisect ladder. Records are written by
 * whichever image last saved, and a ramp through versions can leave a record a
 * later image cannot use -- a NEWER one refuses to guess (INCOMPATIBLE), older
 * ones migrate. Writing defaults at the CURRENT version with the next sequence
 * number supersedes everything before it, so the next boot adopts defaults
 * instead of the unusable record. No erase: the journal appends, and seq wins.
 */
void csd_trim_reset_defaults(void)
{
    tr_pw_timeout_s = TRIM_PW_DEFAULT_S;
    tr_st_defaulted++;
    (void)csd_trim_journal_now(0u);      /* trims 0 dB + timeout, current version */
}

/* Save the value the register writer last applied. Used by the SCPI SAVE
 * command, where the host wants "persist this now" without first setting a new
 * trim. The argument is still passed explicitly -- reading the mailbox here
 * would read tile[0]'s never-written copy (see the journal comment below). */
int csd_trim_save_now(void)
{
    return csd_trim_journal_now(tr_applied_packed);
}

int csd_trim_journal_now(unsigned packed)
{
    static unsigned char sector[4096];
    unsigned res = 0;
    unsigned l = sizeof sector;
    if (!tr_read_sector(sector, l, &res)) {
        tr_st_saved = 2u; tr_st_err = res;
        return 0;
    }

    /* Re-scan rather than trust the boot cursor. Two reasons, both learned
     * the hard way in this project:
     *  1. A failed boot load leaves tr_next_slot at 0 while the sector may
     *     already hold slots -- appending there would program over a
     *     non-erased slot, and a NOR write there produces a slot that is
     *     neither old nor new. The scan finds the REAL first erased slot,
     *     so the store self-heals after any failed load.
     *  2. The cursor is tile[0] state a rig can invalidate by reflashing
     *     under a running firmware; the sector contents are the truth. */
    const trim_slot_t *b = 0;
    unsigned fill = 0;
    unsigned newest = tr_scan(sector, l, &b, &fill);
    tr_next_slot = fill;

    int rewrote = 0;
    if (tr_next_slot >= TRIM_MAX_SLOTS) {
        /* Journal full: erase and restart at slot 0. This is the one path
         * that erases, and it happens once every 128 saves. */
        for (unsigned i = 0; i < sizeof sector; i++) sector[i] = 0xFFu;
        tr_next_slot = 0u;
        rewrote = 1;
    }

    /* packed arrives as an ARGUMENT (see header). Reading the mailbox here
     * was the bug: csd_mb_trim is a plain tile-local static and this function
     * runs on tile[0], so it read tile[0]'s never-written copy and journalled
     * 0,0 forever -- seq climbed, the value did not. Measured 2026-11-01:
     * flash wrote and read correctly (partition=1048576, load=1, seq=4,
     * err=0) while the DAC stayed at 0 dB. */
    /* Newest seq FROM THE SECTOR, not from our cached idea of it. If the
     * boot load failed (or never ran) tr_st_seq is 0 while the sector may
     * hold seq 7 -- starting at 1 would write a record that tr_scan later
     * reads as OLDER than the stale ones, so the trim the operator just set
     * would silently not come back after reboot. The one place where
     * trusting cached state would produce a store that looks healthy and
     * loads the wrong value. */
    unsigned seq = newest + 1u;
    trim_slot_t *s = (trim_slot_t *)(sector + tr_next_slot * TRIM_SLOT_BYTES);
    s->magic       = TRIM_MAGIC;
    s->version_len = (TRIM_VERSION << 16) | 3u;
    s->trim_l_88   = (int32_t)(short)(packed & 0xFFFFu);
    s->trim_r_88   = (int32_t)(short)(packed >> 16);
    s->seq         = seq;
    /* The timeout is written with every journal entry, from the value set by
     * csd_trim_pw_timeout_set() (or adopted from the record at boot). Writing
     * it here rather than in its own entry is what keeps trim and timeout from
     * ever disagreeing after a partial failure. */
    s->pw_timeout_s = tr_pw_timeout_s;
    tr_applied_packed = packed;          /* publish for the single register writer */
    s->chk         = slot_checksum(s);
    s->opt_bits    = tr_ups_en ? TRIM_OPT_UPS : 0u;   /* v3: protected by chk */

    int ok = rewrote ? tr_rewrite_sector(sector, &res)
                     : tr_append_slot(tr_next_slot, sector, &res);
    if (ok) {
        tr_st_saved = 1u;
        tr_st_seq   = seq;
        tr_next_slot++;
        tr_st_save_n++;
    } else {
        tr_st_saved = 2u;
        tr_st_err   = res;
    }
    return ok;
}

/* Cross-tile status accessor: scalar in, scalar out. The rule in this
 * codebase is that nothing crosses a tile boundary by pointer -- see
 * csd_identity.h -- so the caller indexes rather than receiving an array. */
unsigned csd_trim_status_word(unsigned idx)
{
    switch (idx) {
    case 0u: return tr_st_partition;
    case 1u: return tr_st_loaded;
    case 2u: return tr_st_saved;
    case 3u: return tr_st_seq;
    case 4u: return tr_st_err;
    case 5u: return tr_st_load_n;
    case 6u: return tr_st_save_n;
    /* Record-version diagnostics (7..10): which path the loader took. A rig
     * needs these because "the trim did not come back" has four causes and the
     * trim value alone cannot tell them apart:
     *   7 rejected by checksum -> the store used defaults
     *   8 refused: version not understood (a bisect left a newer record)
     *   9 accepted after forward migration (older layout upgraded)
     *  10 reset to defaults (no usable record) */
    case 7u: return tr_st_crc_fail;
    case 8u: return tr_st_unsupported;
    case 9u: return tr_st_migrated + tr_st_migrated_scan;
    case 10u: return tr_st_defaulted;
    default: return 0u;                /* out of range: never read past */
    }
}
