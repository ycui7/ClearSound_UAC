/* L/R trim persistence (TODO item 3, directive 2026-11-01).
 *
 * Plain C: the journal, the validity rules and every quadflashlib call.
 *
 * WHO MAY CALL THESE, AND WHY THAT MATTERS
 * Every fl_* call drives PORT_SQI_* through quadflashlib's SYNCHRONOUS port
 * I/O, and the .xn declares those ports on tile[0]. Issued from a task on
 * another tile they wedge. These entry points are therefore called ONLY
 * from the tile[0] codec server, through AUDIOHW_CMD_TRIM_LOAD / _SAVE /
 * _STAT on the channel it already services -- the same proxy
 * csd_diag_regs.xc uses to reach the I2C bus, whose sole owner is that same
 * task. The tile[1] diagnostic core sends a command and blocks on the
 * reply, which csd_diag_regs.xc documents as legitimate there precisely
 * because that core is not the audio path.
 *
 * Why not a dedicated store task, which was the first design (measured,
 * not theorised): tile[0] is already at 8/8 cores and 10/10 timers with the
 * audio stack -- xmap fails with "Cores available: 8, used: 9 . FAILED /
 * Timers available: 10, used: 11 . FAILED", and [[combinable]] rescues
 * neither (the timer is still reserved). The proxy adds no core and no
 * timer, and its channel's only user is the very task that sets trim, so
 * requests cannot interleave with anything.
 *
 * WHY THE SERVER DOES NOT WEDGE (the identity-module lesson, applied)
 * csd_identity.xc documents a real bench death caused by a blocking
 * resource call ahead of the codec server's select loop. Two differences
 * make this safe here: (1) flash work runs INSIDE a select case, so the
 * server was already serving before any flash call exists; (2) this
 * firmware only executes if the boot ROM just read the SAME chip through
 * the SAME ports -- a QSPI device that has not wedged for the ROM will not
 * wedge for us, and every failure the library CAN report (error codes from
 * connect/read/erase/write) is returned, never spun on. If the chip were
 * dead, the board would not be running at all.
 *
 * Storage lives in the DATA PARTITION of the boot QSPI flash, addressed by
 * OFFSET through fl_readData/fl_writeData -- never by absolute address.
 * See the .c for why, and for what erases it.
 */
#ifndef __csd_trim_store_h__
#define __csd_trim_store_h__

#ifdef __cplusplus
extern "C" {
#endif

/* Give the store its CLAIMED clock-block handle. Must be called by the
 * tile[0] server, which declares the `clock` resource in XC, before the
 * other three entry points. The handle is a resource: only a claimed one
 * may reach quadflashlib, and an unclaimed one does not fail -- it raises a
 * RESOURCE EXCEPTION that would kill the codec server. See the long comment
 * in the .c. Zero (unclaimed) makes every operation refuse with 0x104. */
void csd_trim_set_clkblk(unsigned clk);

/* Adopt the newest VALID record into the mailbox trim word, or leave it at
 * the 0 dB / 0 dB reset default when the store holds none. Idempotent and
 * cheap to re-request; the diagnostic task runs it at startup and a rig can
 * re-run it on demand (VOLume:TRIM:LOAD). */
void csd_trim_boot_load(void);

/* Journal the CURRENT mailbox trim pair. Returns 1 on success, 0 on any
 * flash failure -- which is latched into the status words, never silent. */
int csd_trim_journal_now(void);

/* Status word by index 0..6: partition_bytes, load_status, save_status,
 * seq, last_error, boot_count, save_count. Scalar in / scalar out: the
 * cross-tile rule in this codebase is scalars only, never pointers (see
 * csd_identity.h). Out-of-range index returns 0. */
unsigned csd_trim_status_word(unsigned idx);

#ifdef __cplusplus
}
#endif

#endif /* __csd_trim_store_h__ */
