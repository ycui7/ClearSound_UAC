// Host unit test for the shared ring core (T5). Compile:
//   g++ -O1 -std=c++11 test_ring.cpp -o /tmp/test_ring && /tmp/test_ring
// Verifies csd_dsp_delay_step() is an exact D-sample delay for the real
// per-rate tap counts. This is the same header the firmware compiles.
#include <cstdio>
#include <cstdlib>
#include <vector>
#include "../../src/extensions/csd_dsp_core.h"

static int fail = 0;
#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL: %s\n", msg); fail = 1; } } while (0)

static void test_delay(unsigned n, unsigned fs)
{
    if (n < 3 || (n % 2) == 0) { printf("FAIL: n=%u not odd >=3\n", n); fail = 1; return; }
    std::vector<int32_t> ring(n, 0);
    unsigned head = 0;
    const unsigned D = (n - 1) / 2;
    const int N = 4 * (int)n + 17;
    std::vector<int32_t> x(N), y(N);
    for (int t = 0; t < N; t++) {
        x[t] = (int32_t)(3000 * t - 7);           // arbitrary injective pattern
        y[t] = csd_dsp_delay_step(ring.data(), &head, n, x[t]);
    }
    for (int t = (int)D; t < N; t++) {
        if (y[t] != x[t - (int)D]) {
            printf("FAIL: fs=%u n=%u y[%d]=%d != x[%d]=%d\n",
                   fs, n, t, y[t], t - (int)D, x[t - (int)D]);
            fail = 1; return;
        }
    }
    for (unsigned t = 0; t < D; t++) CHECK(y[t] == 0, "warmup must output zeros");
}

int main()
{
    const unsigned rates[]  = {44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000};
    const unsigned taps[]   = {147, 159, 293, 317, 583, 635, 1165, 1269};
    for (unsigned i = 0; i < 8; i++) test_delay(taps[i], rates[i]);
    test_delay(3, 0); test_delay(5, 0); test_delay(9, 0);   // edges
    printf(fail ? "T5 FAIL\n" : "T5 PASS: ring = exact D-sample delay all rates\n");
    return fail;
}
