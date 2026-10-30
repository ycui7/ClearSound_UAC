// Codec register access for the SCPI diagnostic interface.
//
// WHY A PROXY INSTEAD OF DIRECT I2C
// The ES9219Q bus has exactly one writer in this design: the tile-0 audio-hw
// task (clearsounddac_board.xc, AudioHwRemoteTile0), which also owns mute,
// volume and the PLL/LED sequences. A second I2C client on the diagnostic core
// would race those and corrupt codec state -- audible, intermittent, and the
// worst class of bug to chase. So a SCPI register access is a REQUEST to that
// owner, over a channel index it already services generically
// (`case c[int i] :> unsigned cmd`), which is why this file adds no server-side
// code: the read and write branches already exist there.
//
// The channel is the one the app reserves for this task (c_audiohw[4], bound to
// uc_audiohw_diag in src/core/user_main.h). A dedicated index rather than
// sharing uc_audiohw1 matters: on a shared chanend, our request could
// interleave with the audio core's own request/reply sequence and we would read
// back someone else's answer as a result. For a diagnostic interface, a wrong
// number is worse than an error.
//
// BLOCKING IS LEGITIMATE HERE, AND ONLY HERE
// These calls wait on another core's reply and can stall this task. Acceptable
// because this core is not the audio path. The same behaviour on the audiohub
// thread (a printf, or a block-FIR burst) is what cost us the silent-DAC week.

#include <xs1.h>
#include <stdint.h>
#include "csd_diag.h"
#include "clearsounddac_board.h"

/* Bound in src/core/user_main.h (unsafe cast of c_audiohw[4]). */
extern unsafe chanend uc_audiohw_diag;

/* No lock: this channel has exactly ONE user -- the diagnostic task -- and it
 * is single-threaded, so requests cannot interleave. If a second caller ever
 * appears, that is the moment to add a real lib_locks mutex (server + client),
 * not before. */
/* reply_truncs lives in csd_diag_scpi.c, where truncation actually happens
 * (SCPI_Write). A copy here would be a duplicate symbol at link time. */
static unsigned reg_writes;          /* bench audit: how often a host touched
                                     * the codec through this interface */

void csd_diag_note_reg_write(void)  { reg_writes++; }
unsigned csd_diag_reg_writes(void)  { return reg_writes; }

int csd_diag_reg_read(unsigned reg, unsigned *val)
{
    unsigned v;

    if (reg > 0xFFu) return 0;                   /* 8-bit codec register space */

    unsafe {
        uc_audiohw_diag <: (unsigned) AUDIOHW_CMD_REGRD;
        uc_audiohw_diag <: reg;
        uc_audiohw_diag :> v;                     /* server replies the value */
    }

    *val = v;
    return 1;
}

int csd_diag_reg_write(unsigned reg, unsigned val)
{
    if (reg > 0xFFu || val > 0xFFu) return 0;

    unsafe {
        uc_audiohw_diag <: (unsigned) AUDIOHW_CMD_REGWR;
        uc_audiohw_diag <: reg;
        uc_audiohw_diag <: val;                   /* no reply on the write path */
    }

    reg_writes++;
    return 1;
}
