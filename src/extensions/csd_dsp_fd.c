// CSD DSP FIR engine (B1) — XMOS frequency-domain block FIR.
//
// Kernel and coefficients are XMOS's own: lib_audio_dsp fd_block_fir
// (AN02027, examples/app_fd_block_fir). Coefficients come from
// tools/dsp/gen_fd_fir.py via audio_dsp.dsp.fd_block_fir.fir_block_fd, and
// are validated host-side against the vendor's own float model
// (tools/dsp/verify_fd_stream.py). Nothing here implements DSP maths: this
// file only supplies the frame buffering the block API requires (it consumes
// and produces FRAME_ADVANCE samples at a time, while lib_xua's
// UserBufferManagement hands us one frame per call).
//
// Why FD instead of filter_fir_s32: the single-sample VPU kernel costs 3
// thread-cycles per 8 taps (lib_xcore_math). Production taps (1269 at 384 kHz,
// stereo) then need ~366 MIPS against a 120-MIPS logical core (AN02011 3.2) --
// measured silent >=192 kHz. lib_audio_dsp states the block FD form is ~6x
// cheaper: ~61 MIPS, which fits one thread.
//
// Runs on the audiohub core (called from UserBufferManagement): single writer,
// no locking. Output latency is FRAME_ADVANCE samples, inherent to a block
// convolution at this frame size.
#include <string.h>
#include <stdint.h>
#include "csd_dsp_fd_conf.h"
#ifdef CSD_DSP_DEBUG
#include <xcore/hwtimer.h>
#endif

#ifdef CSD_DSP_DEBUG
#include <stdio.h>
static int fd_boot_printed = 0;
static unsigned fd_blk_printed = 0;      /* blocks seen since last config */
static unsigned fd_blk_max_cycles = 0;   /* worst block burst, reference-time ticks */
#define FDDBG(fmt, ...) do { if (!fd_boot_printed) { printf("CSDFD: " fmt "\n", ##__VA_ARGS__); fd_boot_printed = 1; } } while (0)
/* Block trace: first 3 blocks after each config, then every 100th block, so
 * "never reached a block boundary" is told apart from "latch ate the print"
 * AND mid-stream blocks (with real audio) are visible. */
#define FDBLK(fmt, ...) do { fd_blk_printed++; \
    if (fd_blk_printed <= 3u || (fd_blk_printed % 100u) == 0u) \
        printf("CSDFD: blk#" "%u " fmt "\n", fd_blk_printed, ##__VA_ARGS__); } while (0)
#define FDDBG_ONCE_RESET() do { fd_boot_printed = 0; fd_blk_printed = 0; } while (0)
#else
#define FDDBG(fmt, ...) do { } while (0)
#define FDBLK(fmt, ...) do { } while (0)
#define FDDBG_ONCE_RESET() do { } while (0)
#endif

/* Per-channel kernel state. The fd kernel does VPU (8-byte) accesses through
 * this buffer, so it must be 8-byte aligned. */
static int32_t fd_blob[2][CSD_FD_MAX_DATA_WORDS] __attribute__((aligned(8)));
static fd_fir_data_t fd_data[2];
static fd_fir_filter_t fd_filter;
static int32_t fd_scratch[CSD_FD_MAX_TD_BLOCK_LENGTH] __attribute__((aligned(8)));

static int32_t fd_in[2][CSD_FD_FRAME_ADVANCE];
static int32_t fd_out[2][CSD_FD_FRAME_ADVANCE];        // emitted with 1 frame lag
static unsigned fd_idx;
static int fd_rate = -1;
static int fd_ready = 0;

void csd_dsp_fd_config(int rate_idx)
{
    fd_ready = 0;
    fd_rate = -1;
    if (rate_idx < 0 || (unsigned)rate_idx >= CSD_FD_NUM_RATES) return;

    const csd_dsp_fd_rate_t *r = &csd_dsp_fd_rates[rate_idx];
    /* add_data/compute must run in lock-step at frame_advance granularity;
     * refuse a rate whose generated geometry disagrees. */
    if (r->frame_advance != CSD_FD_FRAME_ADVANCE) return;
    if (r->data_words > CSD_FD_MAX_DATA_WORDS) return;
    if (r->td_block_length > CSD_FD_MAX_TD_BLOCK_LENGTH) return;

    fd_filter.coef_blocks     = (bfp_complex_s32_t *)r->coef_blocks;
    fd_filter.td_block_length = r->td_block_length;
    fd_filter.block_count     = r->block_count;
    fd_filter.taps_per_block  = r->taps_per_block;

    for (unsigned ch = 0; ch < 2; ch++) {
        memset(fd_blob[ch], 0, sizeof fd_blob[ch]);
        fd_block_fir_data_init(&fd_data[ch], fd_blob[ch],
                               r->frame_advance, r->td_block_length,
                               r->block_count);
    }
    fd_idx = 0;
    memset(fd_out, 0, sizeof fd_out);
    memset(fd_in, 0, sizeof fd_in);
    fd_rate = rate_idx;
    fd_ready = 1;
    FDDBG_ONCE_RESET();
    FDDBG("config rate=%u taps=%u tdl=%u blocks=%u tpb=%u fa=%u words=%u",
          r->rate, r->num_taps, r->td_block_length, r->block_count,
          r->taps_per_block, r->frame_advance, r->data_words);
}

void csd_dsp_fd_clear(void) { fd_ready = 0; fd_rate = -1; }
int  csd_dsp_fd_ready(void) { return fd_ready; }

#ifdef CSD_DSP_DIAG_SELFTEST
/* Deterministic, INTEGER-ONLY adapter self-test (no libm, per firmware rule).
 * With the delta tables in the tree, the FD engine must reproduce its input
 * bit-exactly, delayed by (kernel centre + frame_advance). Feed an LCG pattern,
 * search the lag, report best (lag, match-count). Run from the FIRST hook
 * invocation so it executes on the audiohub core -- the context that fails.
 * Returns (lag<<12)|matches; matches==0 => kernel dead in this context. */
#define CSD_ST_BLOCKS 8u
void csd_dsp_fd_frame(unsigned s[2]);        /* defined below */
unsigned csd_dsp_fd_selftest2 = 0xDEADu;     /* (mad>>16 | max>>16), 12+12 bits */
unsigned csd_dsp_fd_selftest3 = 0xDEADu;     /* (last-blk matches | nz>>1)     */
static int32_t st_in[CSD_ST_BLOCKS * CSD_FD_FRAME_ADVANCE];
static int32_t st_out[CSD_ST_BLOCKS * CSD_FD_FRAME_ADVANCE];

unsigned csd_dsp_fd_selftest(unsigned rate_idx)
{
    const unsigned fa = CSD_FD_FRAME_ADVANCE;
    const unsigned total = CSD_ST_BLOCKS * fa;
    const int save_rate = fd_rate;            /* restore live state afterwards */
    unsigned i, lag, best_lag = 0, best_match = 0;
    unsigned seed = 0x12345678u;

    for (i = 0; i < total; i++) {
        seed = seed * 1103515245u + 12345u;
        st_in[i] = (int32_t)(seed & 0xFFFFFFF8u);   /* ~28-bit deterministic */
    }
    csd_dsp_fd_config(rate_idx);
    for (i = 0; i < total; i++) {
        unsigned s[2] = { (unsigned)st_in[i], (unsigned)st_in[i] };
        csd_dsp_fd_frame(s);
        st_out[i] = (int32_t)s[0];                  /* emitted frame sample */
    }
    csd_dsp_fd_config(save_rate >= 0 ? (unsigned)save_rate : rate_idx);

    /* compare only after full pipeline depth (2 blocks) + lag has streamed */
    for (lag = fa / 2u; lag <= fa * 2u; lag++) {
        unsigned m = 0;
        for (i = fa * 2u + lag; i < total; i++)
            if (st_out[i] == st_in[i - lag]) m++;
        if (m > best_match) { best_match = m; best_lag = lag; }
    }
    /* second metric: magnitude error over the FINAL full block at the winning
     * lag. Bit-exactness is NOT expected from an FD engine (block-shared
     * exponent loses low bits relative to the block max), so measure |out-in|
     * normalised >>16 and saturate to 12 bits: a working path gives ~0-64, a
     * corrupt path saturates at 4095. Also census nonzero outputs. */
    {
        uint64_t sum = 0;
        unsigned mx = 0, m128 = 0, nz = 0;
        for (i = total - fa; i < total; i++) {
            int d = (int)(st_out[i] - st_in[i - best_lag]);
            if (d < 0) d = -d;
            sum += (uint64_t)d;
            if ((unsigned)d > mx) mx = (unsigned)d;
            if (st_out[i] == st_in[i - best_lag]) m128++;
            if (st_out[i] != 0) nz++;
        }
        {
            unsigned mad = (unsigned)(sum / fa) >> 16;
            unsigned mxn = mx >> 16;
            if (mad > 4095u) mad = 4095u;
            if (mxn > 4095u) mxn = 4095u;
            csd_dsp_fd_selftest2 = (mad << 12) | mxn;
            csd_dsp_fd_selftest3 = (m128 << 12) | (nz >> 1);
        }
    }
    return ((best_lag & 0xFFu) << 12) | (best_match & 0xFFFu);
}
#endif

void csd_dsp_fd_frame(unsigned s[2])
{
    const csd_dsp_fd_rate_t *r = &csd_dsp_fd_rates[fd_rate];
    const unsigned fa = r->frame_advance;
    int32_t in[2];

    /* 1. capture the host sample BEFORE overwriting the buffer */
    for (unsigned ch = 0; ch < 2; ch++) in[ch] = (int32_t)s[ch];
    /* 2. emit the processed sample that entered FRAME_ADVANCE ago */
    for (unsigned ch = 0; ch < 2; ch++) s[ch] = (unsigned)fd_out[ch][fd_idx];
    /* 3. queue the new sample */
    for (unsigned ch = 0; ch < 2; ch++) fd_in[ch][fd_idx] = in[ch];

    if (++fd_idx < fa) return;
    fd_idx = 0;

    for (unsigned ch = 0; ch < 2; ch++) {
#ifdef CSD_DSP_DEBUG
        unsigned t0 = get_reference_time();
#endif
        fd_block_fir_add_data(fd_in[ch], &fd_data[ch]);
        fd_block_fir_compute(fd_scratch, &fd_data[ch], &fd_filter);
#ifdef CSD_DSP_DEBUG
        {   /* One number decides the architecture: per-block cycles vs the
             * audiohub budget fa * (600MHz/rate). Printed on the #100 block so
             * the stream is definitely live. */
            unsigned cyc = get_reference_time() - t0;
            if (cyc > fd_blk_max_cycles) fd_blk_max_cycles = cyc;
            if (fd_blk_printed < 3u || (fd_blk_printed % 500u) == 0u)
                printf("CSDFD: blk#%u ch%u cyc=%u max=%u in0=%d out=%d\n",
                       fd_blk_printed, ch, cyc, fd_blk_max_cycles,
                       (int)fd_in[ch][0], (int)fd_scratch[r->frame_overlap]);
            if (ch == 1) fd_blk_printed++;
        }
#else
        if (0) { }
#endif
        /* The valid frame is [frame_overlap, frame_overlap+frame_advance) of
         * the compute output -- per lib_audio_dsp test/fd_block_fir/src/main.c */
        memcpy(fd_out[ch], &fd_scratch[r->frame_overlap], fa * sizeof(int32_t));
    }
}
