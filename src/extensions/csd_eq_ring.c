/* Implementation -- see csd_eq_ring.h for the contract and the rules.
 * Witness globals live here (unconditional definitions; increments gated by
 * CSD_EQ_DIAG_WITNESS, same rule as g_csd_bq_* in csd_dsp_bq.c, which proves
 * src/extensions C files do receive CSD_EXTRA_FLAGS). */
#include "csd_eq_ring.h"

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
    if (!engaged) {                       /* engine off: byte-exact bypass  */
        csd_eq_stage_r = r;
        csd_eq_pushed = 0u;
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
        csd_eq_stage_r = r;
        csd_eq_pushed = 0u;
        return l;
    }
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
    cfg_idx = idx;
    cfg_on = eng_on;
    cfg_seq++;                                    /* payload first, seq last  */
}

void csd_eq_engage(unsigned v)
{
    engaged = v;
#ifdef CSD_EQ_DIAG_WITNESS
    g_csd_eq_cfg_adopt++;   /* one adoption per config the task executed */
#endif
}

/* Engine epoch rollover, executed by the TASK at adoption (all it touches
 * are task-owned cursors): drop any stale backlog and rebase done. The
 * producer sees the generation bump in csd_eq_handoff and rebases its own
 * cursors there -- every cursor keeps exactly one writer core. */
void csd_eq_take_resync(void)
{
    unsigned d = wr_pub;
    rd = d;
    done = d;
    gen++;
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
