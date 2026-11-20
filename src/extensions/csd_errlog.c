/* csd_errlog implementation — see csd_errlog.h for the contract, layout and
 * the ownership rules this file obeys.
 *
 * TILE ARCHITECTURE (settled by measurement, 2026-11-19 — read before
 * "simplifying"):
 *   This board shares NO RAM across tiles. The proven cross-core primitives
 *   are (a) channels and (b) plain statics between tasks on the SAME tile
 *   (csd_vol_mailbox.c: 'Both parties are tile[0] tasks'). A malloc'd block
 *   is tile-private memory: tile[1] writes into it are SILENTLY DROPPED —
 *   no exception, no corruption, just gone (measured: the tile[1] producer
 *   entered post, the shared widx never moved). The dp121 'shared heap'
 *   notes describe ENGINE state whose users are all tile[1] tasks; nothing
 *   in this project has ever crossed tile 0<->1 through memory, and the
 *   attempts to do so here (shared-heap FIFO, attach-by-pointer) produced
 *   days of missing records.
 *   THEREFORE: every static below belongs to tile[0]. The codec-server task
 *   calls the mutating entries (tick/boot/drain/cases); the tile[1] diag
 *   core reaches them ONLY through scalar audiohw-channel commands — at most
 *   one argument word in and one word out per command (the proven
 *   PW_TIMEOUT_SAVE/UPS_SET shape; a 3-word-in/33-word-out FMT command
 *   deadlocked the server the same day). Sector contents reach the rig as
 *   sixteen scalar words per slot pair via csd_errlog_status_word(8..23),
 *   decoded host-side.
 *
 * Record (32 B = 8 words), committed word-LAST so a torn record can only
 * ever read as absent:
 *   w[0] = EL_MAGIC | (nonce & 0xFF)        [commit, written last]
 *   w[1] = seq        w[2] = uptime_ms      w[3] = event id
 *   w[4] = a0         w[5] = a1             w[6] = a2
 *   w[7] = nonce | (checksum & ~0xFF),  checksum = XOR(w[1..6])
 * Ev id 0 is the reserved TOMBSTONE (a dropped post placeholder) so the
 * in-order drain never stalls on a slot that will never commit. */
#include <stdint.h>
#include <stdio.h>
#include "csd_errlog.h"
#include "csd_version.h"

#include <xs1.h>
#include <platform.h>         /* PORT_SQI_CS / _SCLK / _SIO from the .xn */
#include <quadflashlib.h>

/* ---- geometry ---------------------------------------------------------- */
#define EL_TRIM_BYTES   0x1000u                 /* sector 0: trim journal    */
#define EL_BASE         0x1000u                 /* our region starts after   */
#define EL_SECTOR       4096u
#define EL_NSECT        8u                      /* 32 KiB reserved           */
#define EL_MIN_PART     (EL_TRIM_BYTES + EL_SECTOR * EL_NSECT)
#define EL_SLOTS_PER_SECTOR (EL_SECTOR / 32u)   /* 128                       */

#define EL_MAGIC        0x4345524CU             /* 'CERL' — cannot collide
                                                 * with XEF/XE boot scan      */
#define EL_NONCE_MASK   0xFFu

/* Error codes (ours, above the quadflashlib range, so the rig can tell
 * whose failure a code is). */
#define EL_ERR_NO_CLKBLK   0xE01u
#define EL_ERR_NO_PART     0xE02u
#define EL_ERR_PART_SMALL  0xE03u
#define EL_ERR_SCRATCH     0xE04u
#define EL_ERR_SCAN_READ   0xE05u
#define EL_ERR_BAD_SECT    0xE06u

/* ---- tile[0] state (plain statics; tile-affinity rule, see header) ------ */
static unsigned el_fifo[16][8];        /* producers: tile[0] code only       */
static volatile unsigned el_widx;      /* producers reserve slots            */
static volatile unsigned el_ridx;      /* drain cursor (single consumer)     */
static volatile unsigned el_nonce;
static volatile unsigned el_drops;     /* posts replaced by tombstone        */
static volatile unsigned el_ms;        /* uptime clock, 1 ms tick            */
static unsigned el_sof_cnt;            /* SOF beats (ep_buffer tile[0])      */

void csd_errlog_sof_beat(void) { el_sof_cnt++; }      /* ep_buffer, tile[0]  */
unsigned csd_errlog_sof_cnt(void) { return el_sof_cnt; } /* board tick       */

static unsigned el_seq;                /* next record seq                    */
static unsigned el_wsec;               /* write sector 0..NSECT-1            */
static unsigned el_wslot;              /* next slot in wsec                  */
static volatile unsigned el_used;
static volatile unsigned el_drains;
static volatile unsigned el_last_err;
static volatile unsigned el_part;

/* Write-verify witnesses (user question 2026-11-19: ARE RECORDS REAL?):
 * after every fl_writeData the drain reads the 32 bytes back and compares.
 * Result so far: writes land (wv_ok advances with used, wv_bad stays 0). */
static volatile unsigned el_wv_ok, el_wv_bad, el_wv_rdfail;
static volatile unsigned el_wv_w0, el_wv_exp;

/* Read cache: one sector, filled by tile[0]; exposed sixteen scalar words at
 * a time through csd_errlog_status_word(8..23). */
static unsigned      el_cache[EL_SECTOR / 4u];
static volatile unsigned el_cache_sec = 0xFFFFFFFFu;
static volatile unsigned el_cache_gen;
static volatile unsigned el_cache_base;   /* first word of the selected pair */

/* Staging ladder for tile[1] posts (board ERRLOG_P0..P3/PGO cases: one word
 * in per command — multi-word-in channel commands are what deadlocked the
 * server in the FMT incident). */
static unsigned el_stage[4];

/* QSPI clock handle: board.xc injects the CLAIMED c_trim_qspi value (the
 * claim is XC-side; to quadflashlib it is a number), csd_trim_store pattern. */
static unsigned el_clkblk;
void csd_errlog_set_clkblk(unsigned clkblk) { el_clkblk = clkblk; }
static fl_QSPIPorts el_ports = { PORT_SQI_CS, PORT_SQI_SCLK, PORT_SQI_SIO, 0 };
static unsigned char el_scratch[EL_SECTOR] __attribute__((aligned(4)));

/* ---- board hook --------------------------------------------------------- */
void csd_errlog_tick_1ms(void) { el_ms++; }     /* tile[0], 1 ms vol_mb tick */

/* ---- USB witnesses (user goal: catch the re-enumeration cause) ----------
 * UserHostActive is lib_xua's WEAK host-state hook (hostactive.c body is
 * empty — overriding changes no behaviour). EP0 (tile[0]) calls it on
 * suspend/host-gone (0) and enumeration/config/resume (1): a direct post
 * runs on tile[0] and is legal (tile-affinity rule). The boot BOOT record
 * after a death + these events give the last-visible truth per episode. */
void UserHostActive(int active)
{
    csd_errlog_post(active ? EVL_EV_USB_UP : EVL_EV_USB_DOWN,
                    (unsigned)active, 0u, 0u);
}

/* ---- checksum/commit relation (producer + verifier, one place) --------- */
static unsigned el_csum(const volatile unsigned *w)
{
    return (w[1] ^ w[2] ^ w[3] ^ w[4] ^ w[5] ^ w[6]) & ~EL_NONCE_MASK;
}
static unsigned el_slot_ok(const unsigned w[8])
{
    if (w[0] != (EL_MAGIC | (w[7] & EL_NONCE_MASK))) return 0u;
    if ((w[7] & ~EL_NONCE_MASK) != el_csum(w))       return 0u;
    return 1u;
}

/* ---- producers (tile[0] code: board tick/cases/boot) -------------------- */
void csd_errlog_post(unsigned ev, unsigned a0, unsigned a1, unsigned a2)
{
    unsigned idx = el_widx;
    el_widx = idx + 1u;
    volatile unsigned *s = el_fifo[idx & 15u];
    unsigned n = ++el_nonce;
    if (idx - el_ridx >= 16u) {
        /* The consumer stopped (only possible when flash itself fails). The
         * reserved slot MUST still commit, or the in-order drain stalls on a
         * hole forever: commit a tombstone and account the loss. */
        s[1] = 0u; s[2] = el_ms; s[3] = 0u;      /* 0 = tombstone            */
        s[4] = ev; s[5] = a0;    s[6] = 0u;      /* kept: what was dropped   */
        s[7] = n | el_csum(s);
        s[0] = EL_MAGIC | (n & EL_NONCE_MASK);
        el_drops++;
        return;
    }
    s[1] = 0u;                                   /* seq stamped by the drain */
    s[2] = el_ms;
    s[3] = ev;  s[4] = a0;  s[5] = a1;  s[6] = a2;
    s[7] = n | el_csum(s);
    s[0] = EL_MAGIC | (n & EL_NONCE_MASK);       /* COMMIT (written last)    */
}

/* tile[1] cores post through the ladder: board cases call stage() four times
 * (one word per command) then commit_post() — every channel transaction is
 * the proven 1-word-in scalar shape. */
void csd_errlog_stage(unsigned i, unsigned v) { if (i < 4u) el_stage[i] = v; }
void csd_errlog_commit_post(void)
{
    csd_errlog_post(el_stage[0], el_stage[1], el_stage[2], el_stage[3]);
}

/* ---- flash helpers (tile[0]) -------------------------------------------- */
static int el_open(void)
{
    if (el_clkblk == 0u) { el_last_err = EL_ERR_NO_CLKBLK; return 0; }
    el_ports.qspiClkblk = el_clkblk;
    int r = fl_connect(&el_ports);
    if (r) { el_last_err = (unsigned)r; fl_disconnect(); return 0; }
    unsigned sz = fl_getDataPartitionSize();
    el_part = sz;
    if (sz == 0u) { fl_disconnect(); el_last_err = EL_ERR_NO_PART;  return 0; }
    if (sz < EL_MIN_PART) { fl_disconnect(); el_last_err = EL_ERR_PART_SMALL;
                            return 0; }
    if (fl_getWriteScratchSize(EL_BASE, EL_SECTOR) > sizeof el_scratch) {
        fl_disconnect(); el_last_err = EL_ERR_SCRATCH; return 0;
    }
    return 1;
}

static unsigned el_append(unsigned sec, unsigned slot, const unsigned w[8])
{
    unsigned off = EL_BASE + sec * EL_SECTOR + slot * 32u;
    int r = fl_writeData(off, 32u, (const unsigned char *)w, el_scratch);
    if (r) { el_last_err = (unsigned)r; return 0u; }
    return 1u;
}

static unsigned el_erase_sector(unsigned sec)
{
    /* +1: partition sector 0 is the trim journal — not ours.
     * fl_eraseDataSector(n) erases the n-th sector of the DATA PARTITION
     * (quadflashlib.h:669; csd_trim_store.c erases n=0 the same way). */
    int r = fl_eraseDataSector((EL_BASE / EL_SECTOR) + sec);
    if (r) { el_last_err = (unsigned)r; return 0u; }
    return 1u;
}

/* Newest valid record across the ring. Caller holds the connect. */
static void el_scan_locked(void)
{
    unsigned best_seq = 0, best_sec = 0, best_slot = 0, any = 0;
    for (unsigned s = 0u; s < EL_NSECT; s++) {
        if (fl_readData(EL_BASE + s * EL_SECTOR, EL_SECTOR,
                        (unsigned char *)el_cache)) {
            el_last_err = EL_ERR_SCAN_READ;
            continue;                            /* scan reads what it can   */
        }
        for (unsigned k = 0u; k < EL_SLOTS_PER_SECTOR; k++) {
            const unsigned *w = &el_cache[k * 8u];
            if (!el_slot_ok(w)) continue;
            any = 1;
            if (w[1] >= best_seq) { best_seq = w[1]; best_sec = s; best_slot = k; }
        }
    }
    el_wsec = 0u; el_wslot = 0u;
    if (!any) { el_seq = 1u; el_used = 0u; return; }
    el_seq = best_seq + 1u;
    el_used = best_seq;
    el_wsec = best_sec;
    if (best_slot + 1u >= EL_SLOTS_PER_SECTOR) { /* sector full: next one    */
        el_wslot = 0u;
        el_wsec  = (best_sec + 1u) % EL_NSECT;
    } else {
        el_wslot = best_slot + 1u;
    }
}

void csd_errlog_boot_init(void)                  /* tile[0], first tick      */
{
    if (el_open()) {
        el_scan_locked();
        fl_disconnect();                         /* bracket per operation    */
    }
    /* Post UNCONDITIONALLY (RAM only): if flash was not ready the drain
     * persists it a millisecond later. */
    const char *v = CSD_FW_VERSION; unsigned h = 2166136261u;
    while (*v) { h = (h ^ (unsigned)(unsigned char)*v) * 16777619u; v++; }
    csd_errlog_post(EVL_EV_BOOT, h, el_seq, 0u);
}

/* ---- drain: the ONLY flash writer (tile[0], 1 ms tick) ------------------ */
unsigned csd_errlog_drain(void)
{
    if (el_ridx == el_widx) return 0u;           /* 1 ms path stays cheap    */
    if (!el_open()) return 0u;
    el_drains++;
    unsigned wrote = 0u;
    while (wrote < 2u && el_ridx != el_widx) {
        volatile unsigned *fp = el_fifo[el_ridx & 15u];
        unsigned w[8];
        for (unsigned i = 0u; i < 8u; i++) w[i] = fp[i];
        if (!el_slot_ok(w)) break;               /* hole: stop, keep order   */
        if (el_wslot >= EL_SLOTS_PER_SECTOR) {   /* advance into next sector */
            el_wslot = 0u;
            el_wsec  = (el_wsec + 1u) % EL_NSECT;
            if (el_used != 0u) {                 /* reusing a consumed sector:
                                                  * erase FIRST (NOR only
                                                  * goes 1->0). Fresh ring
                                                  * (used==0) skips it.      */
                if (!el_erase_sector(el_wsec)) break;
                el_cache_sec = 0xFFFFFFFFu;      /* cache is stale now       */
            }
        }
        w[1] = el_seq;                           /* stamp seq at write time  */
        w[7] = (w[7] & EL_NONCE_MASK) | el_csum((const volatile unsigned *)w);
        if (!el_append(el_wsec, el_wslot, w)) break;
        /* VERIFY the write reached the media: fl_writeData returns 0 without
         * checking bits; a NOR write into not-erased area 'succeeds' and
         * reads back junk. Read the 32 bytes back while still connected. */
        {   unsigned rb[8];
            unsigned off = EL_BASE + el_wsec * EL_SECTOR + el_wslot * 32u;
            if (fl_readData(off, 32u, (unsigned char *)rb)) el_wv_rdfail++;
            else {
                unsigned diff = 0u;
                for (unsigned q = 0u; q < 8u; q++) diff |= (rb[q] ^ w[q]);
                if (diff) { el_wv_bad++; el_wv_w0 = rb[0]; el_wv_exp = w[0]; }
                else      { el_wv_ok++; }
            }
        }
        el_wslot++; el_seq++; el_used++; el_ridx++; wrote++;
    }
    fl_disconnect();
    return wrote;
}

/* ---- read side (tile[0], scalar channel commands) ----------------------- */
/* One 2-word command picks the exposure: arg = sector*64 + slot-pair.
 * The pair's 16 words become the STAT window (i 8..23). */
unsigned csd_errlog_read_sector(unsigned arg)
{
    unsigned sec = arg >> 6, pair = arg & 63u;
    if (sec >= EL_NSECT) { el_last_err = EL_ERR_BAD_SECT; return 0u; }
    if (!el_open()) return 0u;
    int r = fl_readData(EL_BASE + sec * EL_SECTOR, EL_SECTOR,
                        (unsigned char *)el_cache);
    fl_disconnect();
    if (r) { el_last_err = (unsigned)r; return 0u; }
    el_cache_sec = sec;
    el_cache_base = pair * 16u;
    el_cache_gen++;
    return el_cache_gen;
}

unsigned csd_errlog_erase(void)
{
    if (!el_open()) return 0u;
    unsigned ok = 1u;
    for (unsigned s = 0u; s < EL_NSECT && ok; s++) ok = el_erase_sector(s);
    fl_disconnect();
    if (!ok) return 0u;
    el_seq = 1u; el_used = 0u; el_wsec = 0u; el_wslot = 0u;
    el_cache_sec = 0xFFFFFFFFu;
    csd_errlog_post(EVL_EV_TEST, 0u, 0u, 0u);    /* proves the post-erase
                                                  * write path end to end    */
    return 1u;
}

unsigned csd_errlog_cache_sec(void) { return el_cache_sec; }

/* The scalar window (see header): i 0..7 status, 8..23 the first 16 cache
 * words (slot 0 + slot 1 of the loaded sector), 24..28 write-verify,
 * 30..35 FIFO truth. Bulk formatting over the chanend is BANNED (FMT
 * incident, 2026-11-19): scalars only, decode on the rig. */
unsigned csd_errlog_status_word(unsigned i)
{
    if (i >= 8u && i < 24u)  return el_cache[el_cache_base + i - 8u];
    if (i >= 24u && i < 29u) {
        const volatile unsigned *wp = &el_wv_ok;
        return wp[i - 24u];                      /* ok,bad,rdfail,w0,exp     */
    }
    switch (i) {
    case 0u:  return el_part;
    case 1u:  return EL_NSECT;
    case 2u:  return EL_NSECT * EL_SLOTS_PER_SECTOR;
    case 3u:  return el_seq > 0u ? el_seq - 1u : 0u;  /* records written     */
    case 4u:  return el_drops;
    case 5u:  return el_cache_gen;
    case 6u:  return el_last_err;
    case 7u:  return el_drains;
    case 30u: return el_widx;
    case 31u: return el_ridx;
    case 32u: return el_nonce;
    case 33u: return el_fifo[el_ridx & 15u][3];  /* ev at cursor             */
    case 34u: return el_fifo[el_ridx & 15u][0];  /* commit word at cursor    */
    default:  return 0u;
    }
}
