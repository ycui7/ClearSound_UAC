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
#define TRIM_VERSION  1u
#define TRIM_SLOT_BYTES   32u       /* 8 words: hdr(4) + payload(2) + spare(2) */
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

typedef struct {
    uint32_t magic;
    uint32_t version_len;           /* version << 16 | payload words (2) */
    int32_t  trim_l_88;             /* 8.8 dB, 0 .. -8192 */
    int32_t  trim_r_88;
    uint32_t seq;                   /* monotone; newest wins */
    uint32_t chk;                   /* sum of the five words above it */
    uint32_t rsvd[2];
} trim_slot_t;                        /* 32 bytes */

static uint32_t slot_checksum(const trim_slot_t *s)
{
    const uint32_t *w = (const uint32_t *)s;
    uint32_t c = 0;
    for (unsigned i = 0; i < 5u; i++) c += w[i];
    return c;
}

static int slot_valid(const trim_slot_t *s)
{
    if (s->magic != TRIM_MAGIC) return 0;
    if ((s->version_len >> 16) != TRIM_VERSION) return 0;
    if ((s->version_len & 0xFFFFu) != 2u) return 0;
    if (s->chk != slot_checksum(s)) return 0;
    /* Payload contract, re-checked on load: a record that parses but
     * violates the range/grid must not become the volume path's truth. */
    if (s->trim_l_88 > 0 || s->trim_l_88 < -8192 || (s->trim_l_88 % 32)) return 0;
    if (s->trim_r_88 > 0 || s->trim_r_88 < -8192 || (s->trim_r_88 % 32)) return 0;
    return 1;
}

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
        if (slot_valid(s)) {                      /* torn slots are skipped,
                                                   * not reused: NOR writes
                                                   * cannot restore a 1 bit,
                                                   * so a partially-written
                                                   * slot stays dirty. */
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
int csd_trim_boot_load(unsigned *packed_out)
{
    static unsigned char sector[4096];
    unsigned res = 0;
    *packed_out = 0u;                             /* documented reset default */

    unsigned len = sizeof sector;
    if (tr_read_sector(sector, len, &res)) {
        const trim_slot_t *best = 0;
        unsigned fill = 0;
        unsigned seq = tr_scan(sector, len, &best, &fill);
        tr_next_slot = fill;
        tr_st_load_n++;
        if (best) {
            *packed_out = (((unsigned)(uint32_t)best->trim_r_88) << 16)
                        |  ((unsigned)(uint32_t)best->trim_l_88 & 0xFFFFu);
            tr_st_seq    = seq;
            tr_st_loaded = 1u;
        } else {
            tr_st_loaded = 2u;                    /* connected, nothing valid */
        }
    } else {
        tr_st_loaded = 3u;
        tr_st_err    = res;
    }
    /* Caller (tile[1]) applies *packed_out to its OWN mailbox. Returning the
     * load status lets it report what happened without a second round trip. */
    return (int) tr_st_loaded;
}

/* Journal the given pair. The value is an ARGUMENT, never read from the
 * mailbox: this runs on tile[0] and the mailbox static is tile[1] local, so
 * reading it here journalled zeros while seq happily climbed. Returns 1 on
 * success and 0 on any flash failure; the failure code is latched either way,
 * so the caller's reply is honest. */
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
    s->version_len = (TRIM_VERSION << 16) | 2u;
    s->trim_l_88   = (int32_t)(short)(packed & 0xFFFFu);
    s->trim_r_88   = (int32_t)(short)(packed >> 16);
    s->seq         = seq;
    s->chk         = slot_checksum(s);
    s->rsvd[0] = 0u; s->rsvd[1] = 0u;

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
    default: return 0u;                /* out of range: never read past */
    }
}
