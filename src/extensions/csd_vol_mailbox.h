/* Host volume/mute mailbox: ep0 -> codec server, same tile, zero channels.
 *
 * WHY (owner directive 2026-11-04, chanend diet)
 * ep0 (XUA_Endpoint0) and the codec server (AudioHwRemoteTile0) are BOTH
 * tile[0] tasks (lib_xua main.xc: the `on tile[XUA_XUD_TILE_NUM]` par wraps
 * XUA_Buffer and XUA_Endpoint0; XUD_TILE=0). The old path crossed a chanend
 * pair (c_audiohw[1], "uc_audiohw2") to deliver three words that never left
 * the tile -- 2 of tile[0]'s 32 chanends spent on an intra-tile message.
 * A module static is tile-private on XS3: fatal for a CROSS-tile mailbox,
 * exactly right for a same-tile one. Both parties here are tile[0] tasks, so
 * they see the same .dp.bss copy. (The diagnostic core is tile[1]: its proxy
 * stays a channel, csd_diag_regs.xc, for that reason.)
 *
 * SHAPE: scalar getters only, no pointer parameters.
 * A C function with `unsigned*` parameters called from XC does not link
 * (C pointer vs xC pointer; csd_trim_store.h documents the same lesson), so
 * the consumer runs its seqlock check in XC over single-word reads.
 *
 * Producer: ep0 only. Consumer: the codec server's drain tick.
 * Ordering: mute is posted before volume (as the old same-FIFO DAC_MUTE_SET
 * did); the server drains mute first, then volume, preserving the register
 * write order the sole-writer invariant needs.
 *
 * LATENCY: worst case one drain tick (1 ms) after the USB SET_CUR. Old
 * behaviour was also queue-behind-select, so not a regression in practice.
 *
 * Test: tests/test_csd_vol_mailbox.c (host, pthreads): torn-snapshot,
 * lost-last-post, freshness. It caught two real bugs in the first draft
 * (mute lost to an over-broad odd-seq guard; pointer API uncallable from XC).
 */
#ifndef _CSD_VOL_MAILBOX_H_
#define _CSD_VOL_MAILBOX_H_

#ifdef __cplusplus
extern "C" {
#endif

/* ---- producer (ep0, XC-callable: scalars in) ---- */
void csd_vol_mb_post_mute(unsigned muted);
void csd_vol_mb_post_volume(unsigned ch, int a, int dl);

/* ---- consumer (codec server, XC): seqlock over single-word reads.
 * Volume, per channel:
 *   s0 = csd_vol_mb_seq_vol(ch);
 *   if ((s0 & 1) == 0 && s0 != seen) {
 *       a = csd_vol_mb_read_a(ch); dl = csd_vol_mb_read_dl(ch);
 *       if (csd_vol_mb_seq_vol(ch) == s0) { adopt(a, dl); seen = s0; }
 *   }
 * Mute:
 *   s = csd_vol_mb_seq_mute();
 *   if (s != seen_m) { muted = csd_vol_mb_read_mute(); seen_m = s; ... }  */
unsigned csd_vol_mb_seq_mute(void);
unsigned csd_vol_mb_read_mute(void);
unsigned csd_vol_mb_seq_vol(unsigned ch);
int      csd_vol_mb_read_a(unsigned ch);
int      csd_vol_mb_read_dl(unsigned ch);

/* ---- observability ONLY, never a control input.
 * SINGLE-WRITER CONTRACT: posts has one writer (ep0), drains one (server).
 * Tile[0] statics -- a tile[1] reader sees zeros forever (tile-private trap). */
unsigned csd_vol_mb_posts(void);
unsigned csd_vol_mb_drains(void);
void     csd_vol_mb_note_drain(void);

#ifdef __cplusplus
}
#endif

#endif /* _CSD_VOL_MAILBOX_H_ */
