/* Host twin of the shipping resampler: compiles the REAL csd_ups_block.c
 * (fdhost doctrine, same as host_main.c #includes csd_dsp_fd.c), drives the
 * frozen 4-symbol contract exactly as lib_xua's audiohub loop does, and emits
 * the wire stream the DAC would put on I2S. This is where the bit-exact
 * expectation for ratio-2 tests C/D comes from -- not a re-implementation.
 *
 * stdin : raw S32_LE interleaved pairs (the host stream bytes as played)
 * stdin2 arg: --ratio R --in HZ --out HZ  (family-checked by the real code)
 * stdout: raw S32_LE interleaved pairs, wire frames (R words per host frame)
 *
 * Build: bash tools/dsp/ups_host/build.sh
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

/* the app's XC shim for the 10 ns local timer: witness-only here (two reads
 * per block feed s_block_ticks_max); a free-running counter is honest for a
 * host twin that has no timer resource to claim. */
unsigned csd_ups_now(void) { static unsigned t; return ++t; }

#include "csd_ups_block.c"

int main(int argc, char **argv)
{
    unsigned in_hz = 192000u, out_hz = 384000u;
    for (int i = 1; i < argc - 1; i++) {
        if (!strcmp(argv[i], "--in"))  in_hz  = (unsigned)atoi(argv[i + 1]);
        if (!strcmp(argv[i], "--out")) out_hz = (unsigned)atoi(argv[i + 1]);
    }

    /* Control plane in the same order the board uses at stream start:
     * rate note (loads the bank), wire note (arms), codec grant, want. */
    csd_ups_note_stream_rate(in_hz);
    csd_ups_note_wire_rate(out_hz);
    csd_ups_set_enabled(1u);
    csd_ups_set_want(1u);
    if (!csd_ups_active()) {
        fprintf(stderr, "twin refused: active=%u armed=%u in=%u out=%u "
                        "refusals=%u\n", csd_ups_active(), csd_ups_armed(),
                csd_ups_diag_in_hz(), csd_ups_diag_out_hz(),
                csd_ups_diag_refusals());
        return 3;
    }
    const unsigned R = csd_ups_diag_in_hz() ? out_hz / in_hz : 0u;

    int32_t in2[2];
    int32_t out_pair[2];
    unsigned frame_no = 0u;
    /* Wire cadence: one pop per channel per LRCLK frame; the feed hook fires
     * once every R wire frames (UserBufferManagement hook), carrying one
     * host pair. The first block repeats zeros (ring starts empty until 128
     * pairs have arrived) -- a startup transient the content-based alignment
     * discards; steady state pushes exactly R words per host frame. */
    for (;;) {
        if (frame_no % R == 0u) {
            if (fread(in2, sizeof in2, 1, stdin) != 1) break;
            csd_ups_feed(in2[0], in2[1]);
        }
        for (unsigned c = 0u; c < 2u; c++)
            out_pair[c] = (int32_t)csd_ups_slot(c, 0);
        fwrite(out_pair, sizeof out_pair, 1, stdout);
        frame_no++;
    }
    fflush(stdout);
    fprintf(stderr, "twin done: frames=%u blocks=%u pops=%u/%u repeats=%u/%u "
                    "refusals=%u\n", csd_ups_diag_frames(),
            csd_ups_diag_blocks(), csd_ups_diag_pops(0), csd_ups_diag_pops(1),
            csd_ups_diag_repeats(0), csd_ups_diag_repeats(1),
            csd_ups_diag_refusals());
    return 0;
}
