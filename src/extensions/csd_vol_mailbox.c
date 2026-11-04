/* Seqlock mailbox implementation -- see csd_vol_mailbox.h for WHY this
 * exists (chanend diet, owner directive 2026-11-04) and why a tile-private
 * static is the correct, not the broken, choice here.
 *
 * SCALAR GETTERS ONLY: a C function with pointer parameters called from XC
 * does not link (C pointer vs xC pointer -- csd_trim_store.h documents the
 * same lesson). The consumer's seqlock check therefore lives in the XC
 * server task, over single-word getters.
 *
 * Every field is one 32-bit word: loads and stores are atomic on xcore, so
 * torn reads are impossible by width, and the odd/even sequence closes the
 * multi-word window. Both parties are tile[0] tasks: the scheduler preempts
 * but never runs them in lockstep, and stores retire in order, so a reader
 * that sees the new seq necessarily sees the new payload word. */
#include "csd_vol_mailbox.h"

/* Producer state (ep0 writes, codec server reads). The mute payload is one
 * word, so its seq exists only to make "new value" a != against the
 * consumer's cursor. */
static volatile unsigned mb_mute_seq;      /* bumped on every post          */
static volatile unsigned mb_mute_val;      /* 1 = host muted                */

/* Per channel: seq, then the two 8.8 dB terms, one generation. */
static volatile unsigned mb_vol_seq[2];    /* odd = payload in flight       */
static volatile int      mb_vol_a[2];      /* analog term, signed 8.8 dB    */
static volatile int      mb_vol_dl[2];     /* digital term, signed 8.8 dB   */

static volatile unsigned mb_posts;         /* ep0 side counter              */
static volatile unsigned mb_drains;        /* consumer side counter         */

void csd_vol_mb_post_mute(unsigned muted)
{
    /* Single word: value first, then a distinct sequence stamp. Seq is
     * monotone; at one post per SET_CUR, 2^32 is a device lifetime. */
    mb_mute_val = muted;
    mb_mute_seq++;
    mb_posts++;
}

void csd_vol_mb_post_volume(unsigned ch, int a, int dl)
{
    if (ch > 1) return;                    /* caller guards; belt + braces  */
    if ((mb_vol_seq[ch] & 1u) == 0u) mb_vol_seq[ch]++;   /* odd: writing    */
    mb_vol_a[ch]  = a;
    mb_vol_dl[ch] = dl;
    mb_vol_seq[ch]++;                      /* even: snapshot published      */
    mb_posts++;
}

/* ---- single-word reads for the consumer (all callable from XC) ---- */

unsigned csd_vol_mb_seq_mute(void)  { return mb_mute_seq; }
unsigned csd_vol_mb_read_mute(void) { return mb_mute_val; }

unsigned csd_vol_mb_seq_vol(unsigned ch)
{
    return (ch > 1) ? 0u : mb_vol_seq[ch];
}
int csd_vol_mb_read_a(unsigned ch)
{
    return (ch > 1) ? 0 : mb_vol_a[ch];
}
int csd_vol_mb_read_dl(unsigned ch)
{
    return (ch > 1) ? 0 : mb_vol_dl[ch];
}

/* Observability ONLY -- never used for control decisions.
 * SINGLE-WRITER CONTRACT: mb_posts has exactly one writer task (ep0),
 * mb_drains exactly one (the codec server drain). Plain ++ is correct under
 * one writer each and WRONG for any second producer -- the host test races
 * two producers, so its posts count is an undercount by design and asserts
 * nothing; the torn-snapshot property is the one that matters. Both live in
 * tile[0]'s .dp.bss: read them ONLY from a tile[0] task -- a tile[1] reader
 * (the SCPI core) sees zeros forever, the classic tile-private-static trap.
 * If SCPI must report them, proxy through the codec server. */
unsigned csd_vol_mb_posts(void)  { return mb_posts;   }
unsigned csd_vol_mb_drains(void) { return mb_drains;  }
void csd_vol_mb_note_drain(void) { mb_drains++;       }
