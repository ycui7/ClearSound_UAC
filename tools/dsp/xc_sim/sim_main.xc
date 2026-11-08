/* sim_main.xc -- XC entry point for the simulator run.
 *
 * All arithmetic stays in the C translation unit (csd_ups_sim.xc compiled with
 * -DCSD_SIM_STORE, plus the production core). This file crosses into XC only to
 * print, because a C .xe on XS3 produces no stdout while XC's printchar does.
 * The boundary is one unsigned and one int per character: no pointer and no
 * 64-bit type crosses it, which is what the cross-language type check rejects.
 */
#include <print.h>

extern unsigned sim_len(void);
extern int sim_get(unsigned i);
extern int run_all(void);

int main(void)
{
    (void)run_all();
    unsigned n = sim_len();
    for (unsigned i = 0; i < n; i++) printchar((char)sim_get(i));
    return 0;
}
