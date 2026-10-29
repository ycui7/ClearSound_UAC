/* CSD DSP ring core — shared, host-testable (tools/dsp/test_ring.cpp compiles
 * this exact file). No pointers: XC-friendly index math only. */
#ifndef __csd_dsp_core_h__
#define __csd_dsp_core_h__

/* DELAY preset step: y[t] = x[t-D], D=(n-1)/2, ring length n (n odd).
 * head is the write cursor (advance handled here). */
static inline int csd_dsp_delay_step(int32_t *ring, unsigned *head,
                                     unsigned n, int32_t x)
{
    unsigned h = *head;
    const unsigned D = (n - 1u) / 2u;
    unsigned r = h + n - D;
    if (r >= n) r -= n;
    int y = ring[r];
    ring[h] = x;
    h++;
    if (h >= n) h = 0u;
    *head = h;
    return y;
}

#endif
