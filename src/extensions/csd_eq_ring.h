/* Lock-free SPSC ring for the relocated per-frame EQ (2026-11-10).
 *
 * Producer/consumer: the audiohub core (UserBufferManagement -> csd_eq_tx,
 * O(1): bounded stores/loads, no compute, no blocking) and the EQ task core
 * (csd_eq_take_* / csd_eq_take_commit). Lives in C with scalar-only entry
 * points for exactly two reasons, both already proven on this board:
 * XC's whole-program parallel-usage analysis refuses plain globals touched
 * from two par branches ("use of 'usage.anon.N' violates parallel usage
 * rules", raised at main.xc's top-level par), while C-file statics behind
 * function calls are opaque to it -- csd_vol_mailbox.c is the precedent the
 * owner accepted for the same split; and C functions with POINTER parameters
 * do not link when called from XC (csd_trim_store.h lesson), hence the
 * stage-and-read-back shape of the delivery path.
 *
 * Discipline (owner rule: never drop, hold, repeat or silence):
 *   counters are absolute frame numbers (wrap-safe); every enqueued frame
 *   is consumed once and emerges once, in order;
 *   full in-ring (task cannot keep up) => the pair is not enqueued, the
 *   frame passes through unfiltered on the wire side, g_csd_eq_overrun
 *   asserts -- a hard qualification failure, must read 0 in passing runs;
 *   empty out-ring (pipeline fill on enable, or dead task) => the staged
 *   delivery IS the input pair (honest pass-through), g_csd_eq_underrun
 *   asserts -- transient-only in passing runs.
 * Cross-core visibility: every shared word is `volatile` (xcore DP RAM is
 * shared and un-cached; same rule as csd_vol_mailbox.c's statics); each is
 * a single aligned 32-bit access, so torn reads are impossible by width.
 */
#ifndef CSD_EQ_RING_H
#define CSD_EQ_RING_H

#ifdef __cplusplus
extern "C" {
#endif

/* Audio core (UserBufferManagement) side -- ONE call per frame.
 * csd_eq_handoff(l, r) decides + executes the whole per-slot exchange:
 *   engine disengaged      -> returns l, stages r (identity pass-through)
 *   in-ring FULL           -> returns l, stages r, g_csd_eq_overrun++
 *                             (nothing enqueued: honest pass-through)
 *   in-ring ok             -> enqueues the pair; returns the filtered L and
 *                             stages the filtered R when ready, else returns
 *                             and stages the INPUT pair (pipeline fill,
 *                             g_csd_eq_underrun++).
 * The caller unconditionally assigns s[0]=return, s[1]=csd_eq_stage_r and
 * feeds the resampler only when csd_eq_pushed was set by this call.
 * (Plain, not volatile: the stage is written and read exclusively by the
 * audio core -- the cross-core data is the ring arrays behind it. XC also
 * forbids volatile on non-pointer extern declarations.) */
extern unsigned csd_eq_stage_r;   /* staged R of the last handoff            */
extern unsigned csd_eq_pushed;    /* 1 = last handoff enqueued               */
unsigned csd_eq_handoff(unsigned l, unsigned r);

/* Control plane: audio core posts, the task adopts and acknowledges. */
void csd_eq_cfg_post(unsigned eng_on, unsigned idx);

/* EQ task side. */
void csd_eq_engage(unsigned v);
void csd_eq_take_resync(void);   /* task: drop stale backlog, bump epoch    */
unsigned csd_eq_cfg_seq_get(void);
unsigned csd_eq_cfg_on_get(void);
unsigned csd_eq_cfg_idx_get(void);
unsigned csd_eq_take_ready(void);
int  csd_eq_take_l(void);
int  csd_eq_take_r(void);
void csd_eq_take_commit(unsigned l, unsigned r); /* publish filtered pair    */

#ifdef __cplusplus
}
#endif
#endif /* CSD_EQ_RING_H */
