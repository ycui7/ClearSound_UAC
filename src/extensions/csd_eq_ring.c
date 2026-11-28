/* Implementation -- see csd_eq_ring.h for the contract and the rules.
 * Witness globals live here (unconditional definitions; increments gated by
 * CSD_EQ_DIAG_WITNESS, same rule as g_csd_bq_* in csd_dsp_bq.c, which proves
 * src/extensions C files do receive CSD_EXTRA_FLAGS). */
#include "csd_eq_ring.h"
unsigned g_csd_handoff_calls;   /* REVERT(dp172) */

/* EXPERIMENT 11.11.31: the armed-upsampler flow-control wait (REVERT with
 * the tap). csd_ups_armed lives in csd_ups_block.c (same tile[?]; scalar C
 * linkage proven for this pattern). g_csd_eq_stalls counts wait timeouts:
 * a passing armed run reads 0. */
extern unsigned csd_ups_armed(void);
unsigned g_csd_eq_stalls;

#define CSD_EQ_RING_LOG2 6
#define CSD_EQ_RING_LEN  (1u << CSD_EQ_RING_LOG2)
#define CSD_EQ_RING_MASK (CSD_EQ_RING_LEN - 1u)

static volatile int rin[CSD_EQ_RING_LEN][2];   /* host pairs, audio -> task */
static volatile int rout[CSD_EQ_RING_LEN][2];  /* filtered, task -> audio   */
static volatile unsigned wr_pub; /* next in slot to take (task reads)       */
static volatile unsigned rd;     /* next in slot to take     (task writes)  */
static volatile unsigned done;   /* outputs < done are ready (task writes)  */
static unsigned wr_loc;          /* producer cursor: producer-only storage  */
static unsigned ord_loc;         /* consumer cursor: producer-only storage  */
static unsigned pend;            /* est. backlog; refreshed from rd_pub     */
static unsigned gen_loc;         /* last engine epoch the producer observed */
static volatile unsigned gen;    /* engine epoch (task bumps at adoption)   */
static volatile unsigned cfg_seq, cfg_on, cfg_idx, engaged;

/* Staged wire pair for the frame of the last csd_eq_handoff. Written only
 * inside C functions called by the audio core; read by the audio core --
 * single-core storage, no volatile needed (and XC forbids it on externs). */
unsigned csd_eq_stage_r;
unsigned csd_eq_pushed;

/* Witness globals (unconditional definitions; increments gated -- see the
 * file header). Read by csd_diag_scpi.c. REVERT with the ring. */
unsigned g_csd_eq_underrun, g_csd_eq_overrun, g_csd_eq_occ_high;
unsigned g_csd_eq_cfg_adopt;

unsigned csd_eq_handoff(unsigned l, unsigned r)
{
    g_csd_handoff_calls++;   /* REVERT(dp172): producer-side liveness */
    if (!engaged) {                       /* engine off: byte-exact bypass  */
        csd_eq_stage_r = r;
        csd_eq_pushed = 0u;
        /* EXPERIMENT 11.11.33 (REVERT with the tap): armed + !engaged is
         * NOT permission to feed on this core. csd_ups_feed has one legal
         * writer — the EQ task. During adoption/engage windows while the
         * upsampler client is armed, enqueue anyway (the task consumes
         * unconditionally once running: take_ready is a cursor compare, not
         * an engage test), so the audio core NEVER calls feed armed. An
         * unstarted task just lets the ring fill; the armed path below
         * flow-controls. Measured 11.12.8: leaving this path inline gave
         * feed_enter-blocks==1 and a frozen feed_in_pc — dual-writer
         * corruption of the fd FIR state within one adoption window. */
        if (csd_ups_armed()) {
            /* REVERT(csd-armed-passthrough 2026-11-28): flow-control the
             * ARMED band-off enqueue WITHOUT SPINNING. WHY: armed + preset 0
             * is now a steady state (csd_dsp.xc passes the pair here so the
             * EQ task -- the only legal feed writer -- drains it), and an
             * unconditional enqueue into a full ring wraps wr_loc over unread
             * slots, silently reordering the feed.
             * MEASURED: the first form of this guard SPUN on the ring cap
             * (bounded 20 000 iterations) -- that runs on the BIT-TIMED AUDIO
             * CORE, and image bf25d279 (which carried the spin) smeared a
             * 1 kHz tone into a 46.9 Hz comb (peak 952 Hz, margin 18 dB)
             * while the identical build without it was gate-clean (1000.5 Hz,
             * margin 125 dB). A spin here is forbidden by this file's own
             * cross-core wait-cycle note. The guard is therefore a single
             * non-blocking fullness test: full -> DROP this frame (the
             * resampler honest-repeats), never overwrite, never wait. */
            if ((wr_loc - rd) < CSD_EQ_RING_LEN) goto enqueue;
        }
        return l;
    }
    /* New engine epoch (the task bumped gen at adoption): rebase the
     * consumer cursor to the producer so no pre-epoch output is ever
     * delivered (that would replay frames the wire already carried -- a
     * repeat, forbidden), and restart the pipeline fill from zero. */
    unsigned g = gen;
    if (g != gen_loc) { gen_loc = g; ord_loc = wr_loc; pend = 0u; }
    /* Fullness: pend is wr_loc minus the LAST OBSERVED consumer cursor.
     * The task only ever advances rd, so pend can only overestimate the
     * true backlog -- refreshing it every 32 pushes (half of LEN) keeps a
     * 32-frame margin, so the full verdict is never a false negative and a
     * false positive only costs one refresh load. */
    if (pend >= (CSD_EQ_RING_LEN / 2u)) { pend = wr_loc - rd; }
    if (pend >= CSD_EQ_RING_LEN) {        /* task cannot keep up            */
#ifdef CSD_EQ_DIAG_WITNESS
        g_csd_eq_overrun++;               /* qualification failure: must be 0 */
#endif
        /* EXPERIMENT 11.11.31 (REVERT with the tap): while the upsampler
         * client is ARMED, pass-through is not an option — csd_ups_feed has
         * exactly ONE legal writer core (the EQ task, csd_dsp.xc:1020); an
         * audio-core feed on the pass-through path makes two writers of the
         * fd FIR state and the task wedges inside fd_block_fir_compute
         * (measured 11.12.6: task frozen at feed_pc=3 feed_in_pc=1,
         * eq_frames static, overrun climbing). Flow-control instead: wait
         * for a slot, bounded; on timeout fall through (duty emergency,
         * visible via g_csd_eq_stalls). With the race gone the task keeps
         * up (38% duty) and this wait never spins. */
        if (csd_ups_armed()) {
            unsigned spins = 0u;
            while ((pend = wr_loc - rd) >= CSD_EQ_RING_LEN) {
                if (++spins > 20000u) {             /* ~ms-scale bound */
#ifdef CSD_EQ_DIAG_WITNESS
                    g_csd_eq_stalls++;
#endif
                    break;
                }
            }
            if (pend < CSD_EQ_RING_LEN) goto enqueue;
        }
        csd_eq_stage_r = r;
        csd_eq_pushed = 0u;
        return l;
    }
enqueue:;
    unsigned w = wr_loc;
    rin[w & CSD_EQ_RING_MASK][0] = (int)l;
    rin[w & CSD_EQ_RING_MASK][1] = (int)r;
    wr_loc = w + 1u;
    wr_pub = w + 1u;                      /* publish after the words        */
    pend++;
    csd_eq_pushed = 1u;
    unsigned o = ord_loc;
    if (o != done) {
        csd_eq_stage_r = (unsigned)rout[o & CSD_EQ_RING_MASK][1];
        ord_loc = o + 1u;
        return (unsigned)rout[o & CSD_EQ_RING_MASK][0];
    }
    csd_eq_stage_r = r;                   /* pipeline fill: pass-through    */
#ifdef CSD_EQ_DIAG_WITNESS
    g_csd_eq_underrun++;
    {
        unsigned occ = pend << 16 | (done - ord_loc);
        if (occ > g_csd_eq_occ_high) g_csd_eq_occ_high = occ;
    }
#endif
    return l;
}

void csd_eq_cfg_post(unsigned eng_on, unsigned idx)
{
    /* Dedup (2026-11-12): rate flaps re-post the SAME image on every
     * restart; each bump forced a pause/config/resume adoption on the task
     * core and the task wedged inside fd compute on one of 21 such
     * adoptions during boot. Only a changed image may bump the epoch. */
    if (cfg_on == eng_on && cfg_idx == idx) return;
    cfg_idx = idx;
    cfg_on = eng_on;
    cfg_seq++;                                    /* payload first, seq last  */
}

void csd_eq_engage(unsigned v)
{
    engaged = v;
}

/* Engine epoch rollover, executed by the TASK at adoption (all it touches
 * are task-owned cursors): drop any stale backlog and rebase done. The
 * producer sees the generation bump in csd_eq_handoff and rebases its own
 * cursors there -- every cursor keeps exactly one writer core. The adoption
 * witness moved HERE (2026-11-10, pause/load/resume): one adoption = one
 * epoch, and csd_eq_engage is now called twice per adoption (pause then
 * resume), so counting in engage would double every adoption. */
void csd_eq_take_resync(void)
{
    unsigned d = wr_pub;
    rd = d;
    done = d;
    gen++;
#ifdef CSD_EQ_DIAG_WITNESS
    g_csd_eq_cfg_adopt++;   /* one adoption per config the task executed */
#endif
}

unsigned csd_eq_cfg_seq_get(void) { return cfg_seq; }
unsigned csd_eq_cfg_on_get(void)  { return cfg_on; }
unsigned csd_eq_cfg_idx_get(void) { return cfg_idx; }

unsigned csd_eq_take_ready(void) { return rd != wr_pub; }
int csd_eq_take_l(void) { return rin[rd & CSD_EQ_RING_MASK][0]; }
int csd_eq_take_r(void) { return rin[rd & CSD_EQ_RING_MASK][1]; }

void csd_eq_take_commit(unsigned l, unsigned r)
{
    unsigned d = rd;
    rout[d & CSD_EQ_RING_MASK][0] = (int)l;
    rout[d & CSD_EQ_RING_MASK][1] = (int)r;
    rd = d + 1u;
    done = d + 1u;                        /* publish after the words        */
}
