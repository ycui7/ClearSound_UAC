/* Host test of the EXACT shipping FIR path (host-compile alternative to xsim;
 * no board file needed).
 *
 * Compiles the real sources: lib_audio_dsp src/dsp/block_fir/fd_block_fir.c,
 * lib_xcore_math generic-C (src/arch/ref) bfp/FFT kernels, and our
 * csd_dsp_fd.c adapter, against the generated csdfd_<fs>.h tables.
 *
 * Feeds a sine, prints peak output level vs the design expectation. This
 * measures the total scaling of the DSP path (coefficients + kernel + adapter)
 * and localises zero-output bugs to kernel vs adapter.
 *
 * Build+run: bash tools/dsp/fdhost/build_and_run.sh
 */
#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <stdint.h>
#include <string.h>
#include "csd_dsp_fd_conf.h"

/* Vendor headers define (non-static) globals, so the whole shipping path must
 * live in ONE translation unit here — same as the firmware, where only
 * csd_dsp_fd.c includes the generated headers. */
#include "csd_dsp_fd.c"

int main(int argc, char **argv)
{
    unsigned fs = argc > 1 ? (unsigned)atoi(argv[1]) : 48000;
    double f_in = argc > 2 ? atof(argv[2]) : 1000.0;
    double amp  = argc > 3 ? atof(argv[3]) : 0.5;   /* 0.5 -> -6.02 dBFS */
    int n = argc > 4 ? atoi(argv[4]) : 4096;

    int idx = -1;
    for (unsigned i = 0; i < CSD_FD_NUM_RATES; i++)
        if (csd_dsp_fd_rates[i].rate == fs) idx = (int)i;
    if (idx < 0) { printf("no table for %u\n", fs); return 2; }

    const csd_dsp_fd_rate_t *r = &csd_dsp_fd_rates[idx];
    printf("fs=%u taps=%u tdl=%u blocks=%u tpb=%u fa=%u fo=%u words=%u\n",
           fs, r->num_taps, r->td_block_length, r->block_count,
           r->taps_per_block, r->frame_advance, r->frame_overlap, r->data_words);

    csd_dsp_fd_config(idx);
    if (!csd_dsp_fd_ready()) { printf("ADAPTER REFUSED CONFIG (fd_ready=0)\n"); return 3; }

    double peak = 0.0;
    long   nz_count = 0;
    int    first_nonzero = -1, peak_at = -1;
    /* steady-state gain via Goertzel over the tail (skip 8 blocks of transient) */
    const int skip = 8 * (int)r->frame_advance;
    double re = 0.0, im = 0.0, w = 2.0 * 3.14159265358979323846 * f_in / fs;
    long n_meas = 0;
    for (int i = 0; i < n; i++) {
        double v = amp * sin(2.0 * 3.14159265358979323846 * f_in * i / fs);
        int32_t q = (int32_t)lround(v * 2147483647.0);
        unsigned s[2] = { (unsigned)q, (unsigned)q };
        csd_dsp_fd_frame(s);                       /* in -> processed out */
        int32_t y = (int32_t)s[0];
        double a = fabs((double)y / 2147483647.0);
        if (a > peak) { peak = a; peak_at = i; }
        if (y != 0) nz_count++;
        if (y != 0 && first_nonzero < 0) first_nonzero = i;
        if (i >= skip) {
            double yv = (double)y / 2147483647.0;
            re += yv * cos(w * i);
            im += yv * sin(w * i);
            n_meas++;
        }
    }
    double gain = n_meas ? 2.0 * sqrt(re*re + im*im) / (double)n_meas / amp : 0.0;
    printf("in : amp=%.3f (%.2f dBFS) freq=%.0f Hz\n", amp, 20*log10(amp), f_in);
    printf("out: peak=%.6f (%.2f dBFS) nonzero=%ld/%d first_nonzero=%d peak_at=%d\n",
           peak, peak > 0 ? 20*log10(peak) : -999.0, nz_count, n,
           first_nonzero, peak_at);
    printf("gain: %.6f (%+.3f dB)  [Goertzel, %ld samples after %d-sample transient]\n",
           gain, gain > 0 ? 20*log10(gain) : -999.0, n_meas, skip);

    /* Same deterministic self-test the firmware runs on the audiohub core.
     * Ground truth: if HOST also reports ~88/768 bit-matches, that number is
     * normal FD quantization -- not a device fault. */
    {
        extern unsigned csd_dsp_fd_selftest2, csd_dsp_fd_selftest3;
        unsigned st = csd_dsp_fd_selftest(1u);
        printf("selftest(device-metric): lag=%u matches=%u | mad=%u maxerr=%u | lastblk=%u nz2=%u\n",
               (st >> 12) & 0xFF, st & 0xFFF,
               (csd_dsp_fd_selftest2 >> 12) & 0xFFF, csd_dsp_fd_selftest2 & 0xFFF,
               (csd_dsp_fd_selftest3 >> 12) & 0xFFF, (csd_dsp_fd_selftest3 & 0xFFF) * 2);
    }
    return 0;
}
