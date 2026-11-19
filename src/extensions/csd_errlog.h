/* csd_errlog — boot-flash error log for bench death forensics.
 * Copyright 2011—2026, XMOS Ltd, all rights reserved.
 *
 * WHY: the bench board re-enumerates on USB every ~10 minutes (host
 * EHOSTDOWN/-110). Every re-enumeration reboots the firmware and wipes the
 * RAM witnesses (s_probe_pop*, s_rec_*, arm_gen): the very data needed to
 * diagnose the death dies with it. This module journals a compact event
 * stream to the boot-flash DATA partition, which survives reboot, power
 * cycle and reflash (the data partition is rewritten only when a seed file
 * is passed to xflash).
 *
 * LAYOUT of the data partition (offsets in bytes, sector = 4 KiB):
 *   [0x0000, 0x1000)  sector 0: TRIM JOURNAL — owned by csd_trim_store.c.
 *                     NEVER touched here (erasing it would cost the user
 *                     their calibration).
 *   [0x1000, 0x9000)  sectors 1..8: ERRLOG RING — 8 sectors, 1024 slots of
 *                     32 B. Append-only; a sector is erased once before its
 *                     first reuse after wrap. Sector erase granularity
 *                     keeps wear off the trim journal and bounds erase cost
 *                     to one sector per 128 events.
 *
 * RECORD (32 B, 8 words). Committed word-last so a torn record can only
 * read as absent:
 *   w[0] = CERL_MAGIC | (nonce & 0xFF)      [commit marker]
 *   w[1] = seq          (global append counter at write time)
 *   w[2] = uptime_ms    (codec-server ms tick; 0 pre-boot posts)
 *   w[3] = event id     (EVL_EV_*)
 *   w[4..6] = a0,a1,a2  (event args)
 *   w[7] = checksum     (XOR of w[1..6] plus nonce echo)
 * A record is VALID iff w[0] == CERL_MAGIC|(w[7]&0xFF) and
 * w[7] == (w[1]^w[2]^w[3]^w[4]^w[5]^w[6]) | (w[0]&0xFF).
 *
 * DISCIPLINE (each clause bought by an incident, 2026-11-18/19):
 *  - every fl_* call runs on tile[0] (the QSPI ports live there): the ONLY
 *    flash callers are drain()/read_sector()/erase(), reached from the codec
 *    server's select loop or its ERRLOG channel cases;
 *  - fl_connect()/fl_disconnect() bracket each operation, disconnect on
 *    EVERY exit (shared-port rule);
 *  - buffers static (never allocation-fail at erase/write time);
 *  - one translation unit for fl_* (SDK single-instance statics — the
 *    csd_trim_store.c death note);
 *  - tile[0] owns ALL state (plain statics): this board shares no RAM
 *    across tiles, see the architecture note in csd_errlog.c;
 *  - the tile[1] diag core reaches the module ONLY through scalar
 *    audiohw-channel commands, one word in / one word out each (the FMT
 *    multi-word command deadlocked the codec server, 2026-11-19);
 *  - the boot scan reads (never writes); drain() verifies every written
 *    record by reading it back from the media (wv_* witnesses).
 */
#ifndef CSD_ERRLOG_H
#define CSD_ERRLOG_H

#ifdef __XC__
extern "C" {                                /* C body, XC caller (trim rule) */
#endif

/* Event ids: fixed numbers — the rig parses them, never renumber. */
#define EVL_EV_BOOT       1u   /* codec server reached its loop. a0=version
                                * hash, a1=partition words seen              */
#define EVL_EV_ARM        2u   /* csd_ups_set_want(1). a0=host hz, a1=wire
                                * hz, a2=ratio                               */
#define EVL_EV_DISARM     3u   /* csd_ups_set_want(0). same args             */
#define EVL_EV_RECONFIG   4u   /* AudioHwConfig ran. a0=hz a1=ratio          */
#define EVL_EV_STALL      5u   /* enabled but frames frozen >= 1.5 s. a0=fr  */
#define EVL_EV_RESUME     6u   /* frames advancing again.            a0=fr   */
#define EVL_EV_TEST      99u   /* injected via DIAG:ERRLOG:TEST. a0=id       */
#define EVL_EV_ENABLED   11u   /* client enabled 0->1. a0=prev a1=in_hz      */
#define EVL_EV_DISABLED   12u  /* client enabled 1->0                        */

/* Post from tile[0] code (board task, boot, board cases): RAM only, never
 * blocks, never touches the ports. Tile[1] cores use the ladder below
 * (board cases ERRLOG_P0..P3/PGO), NEVER this function — a tile[1] call
 * writes that tile's private copy of the FIFO and is lost forever (the
 * missing-TEST bug, 2026-11-19). */
void csd_errlog_post(unsigned ev, unsigned a0, unsigned a1, unsigned a2);

/* Ladder for tile[1] producers: board cases stage one word per channel
 * command, then commit — every channel transaction keeps the proven
 * 1-word-in scalar shape. */
void csd_errlog_stage(unsigned i, unsigned v);   /* i < 4                    */
void csd_errlog_commit_post(void);

/* Board hook: the codec server's 1 ms tick advances the uptime clock. */
void csd_errlog_tick_1ms(void);

/* Wire the claimed tile[0] QSPI clock block handle in before any flash
 * call — same pattern as csd_trim_set_clkblk. */
void csd_errlog_set_clkblk(unsigned clkblk);

/* tile[0] ONLY (codec server): move at most 2 committed posts to flash
 * (from the 1 ms volume-mailbox tick); returns records written. */
unsigned csd_errlog_drain(void);

/* tile[0] ONLY: load one ring sector into the read cache. Reply word is
 * the cache generation (0 = failed). */
unsigned csd_errlog_read_sector(unsigned sec);

/* Which sector currently sits in the read cache (0xFF.. = none). */
unsigned csd_errlog_cache_sec(void);

/* tile[0] ONLY: erase all 8 ring sectors (DIAG:ERRLOG:ERASE). Reply 1 = ok. */
unsigned csd_errlog_erase(void);

/* tile[0] ONLY, once at codec-server boot: scan the ring, set the append
 * cursor, post EV_BOOT. Called from the board loop after select is proven
 * running (never before — trim header rule). */
void csd_errlog_boot_init(void);

/* The single scalar read path for status AND data (board ERRLOG_STAT case,
 * one word per channel command):
 *   i 0..7   partition, ring sectors, slots, records written, drops,
 *            cache loads, last error, drain rounds
 *   i 8..23  first 16 words of the loaded sector (= slots 0 and 1 of the
 *            sector chosen by ERRLOG_RSEC); the rig decodes 8-word records
 *            (magic/nonce/checksum) host-side
 *   i 24..28 write-verify: ok, bad, read-fail, last-read-w0, last-exp-w0
 *   i 30..34 FIFO truth: widx, ridx, nonce, ev-at-cursor, commit-at-cursor
 * Bulk formatting over the chanend is BANNED (FMT incident): scalars only. */
unsigned csd_errlog_status_word(unsigned i);

#ifdef __XC__
}
#endif

#endif /* CSD_ERRLOG_H */
