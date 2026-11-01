/* L/R trim persistence (TODO item 3, directive 2026-11-01).
 *
 * Stores the two-channel trim pair so it survives power cycles, on the
 * board's boot QSPI flash. OTP is OFF LIMITS by standing owner directive:
 * nothing here reads or writes OTP (csd_identity.xc owns OTP reads, and it
 * is read-only by rule).
 *
 * WHY A JOURNAL IN ONE SECTOR
 * The record is 16 bytes and rewritten only when the factory sets a trim,
 * but flash gives no free write: a byte can only go 1->0, so an update
 * needs an erase, and an erase is the whole 4 KiB sector. Power-loss in the
 * middle of an erase-then-write is the only way this can corrupt, so the
 * store never overwrites in place: it APPENDS fixed 32-byte slots through
 * the sector and a load picks the newest VALID slot. A torn slot fails its
 * checksum and the previous complete slot wins -- the same reason a NOR
 * filesystem journals instead of patching. When the sector fills, erase and
 * restart from slot 0 (the newest record is re-written first, so the erase
 * window loses at most a record that was about to be written again).
 *
 * VALIDITY, per the directive: "default both to 0 dB upon reset/power up,
 * if no valid persistence found" -- validity is magic + version + length +
 * checksum, never "the flash is not erased". An all-0xFF sector is the
 * erased state AND would parse as counts 0xFFFFFF, so an erased-only test
 * is not a validity test.
 *
 * FLASH ACCESS IS COMPILED OUT UNTIL THE LAYOUT IS PROVEN.
 * Writing an arbitrary address in the boot flash can brick the board (the
 * boot ROM scans for XEF blocks; hitting one by accident, or landing on a
 * partition xflash uses, is unrecoverable without a jig). CSD_TRIM_FLASH_ADDR
 * is therefore not defined in the tree, and stays undefined until the
 * layout audit names an address with evidence. With it undefined:
 *   - the mailbox word still works, so the trim FUNCTION (item 4, applied
 *     on every volume change) is live and testable;
 *   - nothing ever writes flash;
 *   - the load always reports "no valid record" -> 0 dB / 0 dB, exactly
 *     the documented reset default.
 * The backend calls below are the quadflashlib sequence used by lib_dfu's
 * runtime writer (dfu_flashlib_user.c), which is the proven pattern for
 * touching this chip from running firmware.
 */
#include <stdint.h>
#include "csd_trim_store.h"
#include "csd_gen.h"          /* csd_mb_get_trim / csd_mb_put_trim */

#ifdef CSD_TRIM_FLASH_ADDR
#include <quadflashlib.h>
#include <platform.h>         /* PORT_SQI_CS / _SCLK / _SIO from the .xn */
#endif

#define TRIM_MAGIC    0x43535254u   /* 'TRS' + N: not the XE magic, not the
                                     * XEF magic -- see the ROM-scan note in
                                     * the layout audit. Deliberately chosen
                                     * away from both. */
#define TRIM_VERSION  1u
#define TRIM_SLOT_BYTES 32u         /* 8 words: hdr(4) + payload(2) + spare(2) */
#define TRIM_MAX_SLOTS  128u        /* 4 KiB sector / 32 */

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

#ifdef CSD_TRIM_FLASH_ADDR

/* Ports: the same three the .xn declares for bootFlash. The clock block is
 * the open question the layout audit answers (lib_dfu uses
 * CLKBLK_DFU_FLASHLIB for its own runtime writer); until then this cannot
 * be enabled anyway -- see CSD_TRIM_FLASH_ADDR above. */
static fl_QuadFlashSpec fl_trim;
static unsigned trim_next_slot;       /* append cursor, set by the load */

static int trim_flash_read(unsigned addr, void *dst, unsigned len)
{
    fl_res r = fl_read(&fl_trim, addr, (unsigned char *)dst, len);
    return (r == FL_RES_OK) ? 0 : -1;
}

static int trim_flash_write_sector(unsigned addr, const unsigned char *buf,
                                   unsigned len)
{
    if (fl_sectorErase(&fl_trim, addr) != FL_RES_OK) return -1;
    /* Pages are 256 B; write the sector in page units (the journal fills a
     * sector linearly, so a whole-sector rewrite is the only path here and
     * it keeps erase/write/verify to one code path). */
    for (unsigned off = 0; off < len; off += 256u) {
        unsigned n = (len - off < 256u) ? (len - off) : 256u;
        if (fl_pageWrite(&fl_trim, addr + off, buf + off, n) != FL_RES_OK)
            return -1;
    }
    return 0;
}

void csd_trim_load_boot(void)
{
    static unsigned char sector[4096];

    if (fl_connect(&fl_trim) != FL_RES_OK) {
        csd_mb_put_trim(0u);          /* documented reset default */
        return;
    }
    if (trim_flash_read(CSD_TRIM_FLASH_ADDR, sector, sizeof sector) != 0) {
        csd_mb_put_trim(0u);
        return;
    }

    const trim_slot_t *best = 0;
    uint32_t best_seq = 0;
    unsigned fill = 0;                /* first free slot for the next append */
    for (unsigned i = 0; i < TRIM_MAX_SLOTS; i++) {
        const trim_slot_t *s = (const trim_slot_t *)(sector + i * TRIM_SLOT_BYTES);
        uint32_t w0 = *(const uint32_t *)s;
        if (w0 == 0xFFFFFFFFu) { fill = i + 1u; break; }   /* erased: append point */
        if (!slot_valid(s)) continue;                        /* torn or foreign */
        if (!best || (int32_t)(s->seq - best_seq) > 0) {     /* wraps correctly */
            best = s;
            best_seq = s->seq;
        }
        fill = i + 1u;
    }

    if (!best) {
        csd_mb_put_trim(0u);          /* no valid persistence -> 0 dB / 0 dB */
        trim_next_slot = fill;
        return;
    }
    unsigned packed = (((unsigned)(uint32_t)best->trim_r_88) << 16)
                    |  ((unsigned)(uint32_t)best->trim_l_88 & 0xFFFFu);
    csd_mb_put_trim(packed);          /* boot adopts the persisted pair */
    trim_next_slot = fill;
}

void csd_trim_request_save(void)
{
    /* Called from the SCPI writer thread only (single writer). Build the
     * whole sector image first, then erase+write: the window in which a
     * power loss loses data is the erase, and what it loses is the record
     * being written -- the same value the caller just set, so re-issuing
     * VOLume:TRIM after a boot recovers it. */
    static unsigned char sector[4096];

    if (fl_connect(&fl_trim) != FL_RES_OK) return;
    if (trim_flash_read(CSD_TRIM_FLASH_ADDR, sector, sizeof sector) != 0) return;

    if (trim_next_slot >= TRIM_MAX_SLOTS) {
        /* Sector full: start over. Re-read is unnecessary -- we are about
         * to erase it all -- but we must place the new record at slot 0 so
         * a load finds it. */
        for (unsigned i = 0; i < sizeof sector; i++) sector[i] = 0xFFu;
        trim_next_slot = 0u;
    }

    unsigned packed = csd_mb_get_trim();
    trim_slot_t *s = (trim_slot_t *)(sector + trim_next_slot * TRIM_SLOT_BYTES);
    uint32_t prev_seq = 0;
    {   /* newest existing seq, so the new record is strictly newer */
        for (unsigned i = 0; i < TRIM_MAX_SLOTS; i++) {
            const trim_slot_t *o = (const trim_slot_t *)(sector + i * TRIM_SLOT_BYTES);
            if (*(const uint32_t *)o == 0xFFFFFFFFu) break;
            if (slot_valid(o)) prev_seq = o->seq;
        }
    }
    s->magic        = TRIM_MAGIC;
    s->version_len  = (TRIM_VERSION << 16) | 2u;
    s->trim_l_88    = (int32_t)(short)(packed & 0xFFFFu);
    s->trim_r_88    = (int32_t)(short)(packed >> 16);
    s->seq          = prev_seq + 1u;
    s->chk          = slot_checksum(s);
    s->rsvd[0] = 0u; s->rsvd[1] = 0u;

    if (trim_flash_write_sector(CSD_TRIM_FLASH_ADDR, sector,
                                sizeof sector) != 0) return;
    trim_next_slot++;
}

#else  /* !CSD_TRIM_FLASH_ADDR -- storage compiled out, function unaffected */

void csd_trim_load_boot(void)
{
    /* No proven flash region yet: report "no valid persistence found" and
     * let the documented default stand. The mailbox word is already 0/0 at
     * reset, so this is an explicit no-op, not an oversight. */
}

void csd_trim_request_save(void)
{
    /* Deliberately empty: the trim pair is live for this power cycle and
     * applied on every volume change (item 4), but it will NOT survive a
     * reboot until CSD_TRIM_FLASH_ADDR is defined with the layout audit's
     * address. Silently not persisting is the safe half of the contract;
     * writing an unproven address is the unrecoverable half. */
}

#endif /* CSD_TRIM_FLASH_ADDR */
