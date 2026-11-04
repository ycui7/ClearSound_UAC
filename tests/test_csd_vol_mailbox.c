/* Host unit test for csd_vol_mailbox.c -- seqlock correctness under
 * adversarial preemption, against the SAME scalar getters the XC server
 * calls. Compile+run natively:
 *   gcc -O2 -I src/extensions -pthread tests/test_csd_vol_mailbox.c \
 *       src/extensions/csd_vol_mailbox.c -o /tmp/tvol && /tmp/tvol
 *
 * Properties (what the codec register path depends on -- a torn snapshot =
 * wrong register value = loud):
 *  1. No torn reads: every adopted pair (a,dl) is one some single post
 *     published. Posts use the correlated pattern a=k*M, dl=k*M+1 (M prime),
 *     so any mismatched pair is detectable. The adopt path replicates the
 *     XC server's seqlock check VERBATIM (server_poll below) -- if it
 *     diverges from clearsounddac_board.xc's vol_mb_tmr case, this test
 *     proves nothing about the firmware.
 *  2. No lost last value: after producers join, the final post per channel
 *     is eventually adopted.
 *  3. Mute visibility: every post changes what the consumer sees.
 *  4. Freshness: a post is never reported new twice.
 *
 * History: the first draft of the module failed properties 3 (mute lost --
 * an over-broad odd-seq guard on a single-word field) and the pointer-API
 * shape could not be called from XC at all. Keep this file as the gate.
 */
#include <stdio.h>
#include <stdlib.h>
#include <pthread.h>
#include <stdint.h>
#include "csd_vol_mailbox.h"

#define M        1000003u          /* prime: (dl-1)%M == a%M iff same post  */
#define NPOST    200000u

static volatile int stop_flag;

static void *producer(void *arg)
{
    unsigned ch = (unsigned)(uintptr_t)arg;
    for (unsigned k = 1; k <= NPOST; k++)
        csd_vol_mb_post_volume(ch, (int)(k * M), (int)(k * M + 1));
    return NULL;
}

static unsigned torn;
static unsigned adopted[2];

/* Verbatim copy of the server drain body (clearsounddac_board.xc,
 * vol_mb_tmr case). Keep the two in lockstep. */
static int server_poll(unsigned ch, unsigned *seen, int *a, int *dl)
{
    unsigned s0 = csd_vol_mb_seq_vol(ch);
    if ((s0 & 1u) == 0u && s0 != *seen) {
        int ca = csd_vol_mb_read_a(ch);
        int cd = csd_vol_mb_read_dl(ch);
        if (csd_vol_mb_seq_vol(ch) == s0) {   /* stable */
            *seen = s0;
            *a = ca; *dl = cd;
            return 1;
        }
    }
    return 0;
}

static void *consumer(void *arg)
{
    (void)arg;
    unsigned seen[2] = {0, 0};
    int a, dl;
    while (!stop_flag) {
        for (unsigned ch = 0; ch < 2; ch++) {
            if (server_poll(ch, &seen[ch], &a, &dl)) {
                adopted[ch]++;
                if ((unsigned)(dl - 1) % M != (unsigned)a % M) torn++;
            }
        }
    }
    return NULL;
}

int main(void)
{
    pthread_t p0, p1, c;

    /* --- properties 1+2: concurrent stress --- */
    stop_flag = 0;
    pthread_create(&c,  NULL, consumer,  NULL);
    pthread_create(&p0, NULL, producer, (void *)(uintptr_t)0);
    pthread_create(&p1, NULL, producer, (void *)(uintptr_t)1);
    pthread_join(p0, NULL);
    pthread_join(p1, NULL);
    {
        unsigned seen[2] = {0, 0};
        int a, dl;
        while (server_poll(0, &seen[0], &a, &dl)) ;
        while (server_poll(1, &seen[1], &a, &dl)) ;
        /* property 4: nothing new without a post */
        if (server_poll(0, &seen[0], &a, &dl)) { puts("FAIL: fresh twice"); return 1; }
        /* final posts must be adoptable */
        csd_vol_mb_post_volume(0, (int)((NPOST+1) * M), (int)((NPOST+1) * M + 1));
        csd_vol_mb_post_volume(1, (int)((NPOST+1) * M), (int)((NPOST+1) * M + 1));
        int got0 = 0, got1 = 0;
        for (int t = 0; t < 1000 && !(got0 && got1); t++) {
            if (!got0 && server_poll(0, &seen[0], &a, &dl) &&
                dl == (int)((NPOST+1) * M + 1)) got0 = 1;
            if (!got1 && server_poll(1, &seen[1], &a, &dl) &&
                dl == (int)((NPOST+1) * M + 1)) got1 = 1;
        }
        if (!got0 || !got1) { puts("FAIL: last post lost"); return 1; }
    }
    stop_flag = 1;
    pthread_join(c, NULL);
    if (torn) { printf("FAIL: %u torn snapshots\n", torn); return 1; }

    /* --- properties 3+4: mute --- */
    {
        unsigned sm = 0;
        csd_vol_mb_post_mute(1);
        if (csd_vol_mb_seq_mute() == sm)      { puts("FAIL: mute not visible"); return 1; }
        if (csd_vol_mb_read_mute() != 1)      { puts("FAIL: mute value wrong"); return 1; }
        sm = csd_vol_mb_seq_mute();
        if (csd_vol_mb_seq_mute() != sm)      { puts("FAIL: reported new twice"); return 1; }
        csd_vol_mb_post_mute(0);
        if (csd_vol_mb_seq_mute() == sm)      { puts("FAIL: unmute lost"); return 1; }
        if (csd_vol_mb_read_mute() != 0)      { puts("FAIL: unmute value wrong"); return 1; }
    }

    printf("PASS: no torn snapshots, adopted=%u+%u, drains=%u\n",
           adopted[0], adopted[1], csd_vol_mb_drains());
    return 0;
}
