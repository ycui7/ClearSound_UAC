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

/* ---- L/R trim persistence proxy (TODO item 3) --------------------------
 * Same channel, same single-user argument as the register calls above: the
 * only caller is this task. These exist because quadflashlib needs the
 * tile[0] SQI ports; see csd_trim_store.h for the measurements that ruled
 * out a dedicated task. Blocking here is legitimate for the same reason the
 * register calls may block -- this core is not the audio path.
 *
 * Plain locals on every send/receive: nothing relies on the channel
 * operators accepting an expression or a struct member. */
/* LOAD returns the packed pair rather than applying it. The store runs on
 * tile[0] and csd_mb_trim is a static that lives on ONE tile.
 *
 * HISTORY of who that tile is, because both readings of "the right tile"
 * were correct in their own model and the flip is the persistence bug this
 * module is famous for:
 *   - mixer-consumer era: the mixer on tile[1] multiplied every frame by the
 *     trim, so tile[1] had to own the mirror. A revision that had the tile[0]
 *     store call csd_mb_put_trim() itself wrote tile[0]'s copy and left the
 *     mixer at 0 dB forever (measured: flash verified good, read-back [0,0],
 *     mic -0.95 dB instead of -8). LOAD therefore returned the value to
 *     tile[1] and the caller published it there.
 *   - register-setup era (directive 2026-11-04): the trim is ES9219Q MASTER
 *     TRIM, not per-frame math. The mixer no longer reads the mirror; its
 *     readers are VOLume:TRIM? (tile[0] SCPI) and the dirty-flag consumer in
 *     csd_AudioHwInit (tile[0]) that writes the chip. tile[0] is now the
 *     right tile, and PUBLISH exists to put the boot value there.
 * LOAD/SAVE still round-trip scalars through here because quadflashlib must
 * run on tile[0] (PORT_SQI_* are declared on that tile in the .xn).
 *
 * Two reply words (packed, status) because 0 is a legitimate packed value:
 * a single word could not tell "adopted the reset default" from "failed". */
int csd_diag_trim_load(unsigned *packed_out)
{
    unsigned packed = 0u, st = 0u;
    unsafe {
        uc_audiohw_diag <: (unsigned) AUDIOHW_CMD_TRIM_LOAD;
        uc_audiohw_diag :> packed;
        uc_audiohw_diag :> st;
    }
    if (packed_out) *packed_out = packed;
    return st ? 1 : 0;
}


/* SAVE takes the pair as an argument for the same reason: tile[0] must never
 * read the tile[1] mailbox to find out what to journal. */
/* Owner case 14: the idle-power timeout is persisted in the SAME journal
 * record as the trims, so saving it takes the trim pair too -- tile[0] must
 * not read the tile[1] mailbox, and one record cannot half-fail. */
int csd_diag_pw_timeout_save(int sec, unsigned packed)
{
    unsigned ok;
    unsafe {
        uc_audiohw_diag <: (unsigned) AUDIOHW_CMD_PW_TIMEOUT_SAVE;
        uc_audiohw_diag <: (unsigned) sec;
        uc_audiohw_diag <: packed;
        uc_audiohw_diag :> ok;
    }
    return ok ? 1 : 0;
}

/* Ask tile[0] to re-run its single volume applier. Used after a trim change:
 * the per-channel term of the trim is an INPUT to that applier (it is the only
 * writer of regs 15/16), so publishing the trim is not enough -- nothing would
 * rewrite the registers until the host volume happened to move. Going through
 * the applier rather than writing the registers here is deliberate: a second
 * writer would race the applier's dedupe cache, which then swallows writes it
 * believes are already applied. */
void csd_diag_vol_reapply(void)
{
    unsafe {
        uc_audiohw_diag <: (unsigned) AUDIOHW_CMD_VOL_REAPPLY;
    }
}

int csd_diag_pw_timeout_get(void)
{
    unsigned v = 300u;                     /* documented default, not a guess */
    unsafe {
        uc_audiohw_diag <: (unsigned) AUDIOHW_CMD_PW_TIMEOUT_GET;
        uc_audiohw_diag :> v;
    }
    return (int) v;                        /* -1 (never) arrives as 0xFFFFFFFF */
}

int csd_diag_trim_save(unsigned packed)
{
    unsigned ok;
    unsafe {
        uc_audiohw_diag <: (unsigned) AUDIOHW_CMD_TRIM_SAVE;
        uc_audiohw_diag <: packed;
        uc_audiohw_diag :> ok;
    }
    return ok ? 1 : 0;
}

unsigned csd_diag_trim_status(unsigned idx)
{
    unsigned v;
    unsafe {
        uc_audiohw_diag <: (unsigned) AUDIOHW_CMD_TRIM_STAT;
        uc_audiohw_diag <: idx;
        uc_audiohw_diag :> v;
    }
    return v;
}

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

/* Unit identity, fetched from the tile[0] owner.
 *
 * WHY THIS EXISTS AT ALL. The identity statics live in .dp.bss, i.e. DP/CP
 * relative and private to one tile. The identity thread runs on tile[0]; the
 * SCPI handlers run on the tile-1 diagnostic core. When those handlers read the
 * statics directly they read tile 1's never-written copy, so every query
 * reported "nothing read" forever -- and the staged progress counter, the very
 * instrument built to say where a read stalled, reported 0 no matter what
 * happened. A per-tile static is not a shared variable, and the compiler says
 * nothing.
 *
 * So ask the owner, over the same chanend REGister:READ? uses. Wire order must
 * match the AUDIOHW_CMD_IDENTITY branch in clearsounddac_board.xc exactly.
 *
 * Blocks on tile[0], which is fine here and nowhere else: this core is not the
 * audio path. It does NOT block on the identity thread, because the server only
 * reads memory -- a wedged OTP read cannot stall this reply. */
#ifdef __XC__
int csd_diag_identity_snapshot(csd_id_snapshot_t * unsafe snap)
#else
int csd_diag_identity_snapshot(csd_id_snapshot_t *snap)
#endif
{
    /* Every transfer goes through a plain local. Two reasons, both learned the
     * hard way here:
     *   - the parameter cannot be called `out`: that is an XC keyword (the
     *     out/in/shared annotation), and using it parses as an annotation and
     *     then "parameter name omitted";
     *   - the channel operators are used on bare variables everywhere else in
     *     this file, so struct members and expressions are not attempted here
     *     either. Receiving into a local and assigning is two instructions and
     *     removes a class of "does the parser accept this" from the build. */
    unsigned w[CSD_ID_STR_WORDS];
    unsigned ready = 0, stage = 0, source = 0, hi = 0, lo = 0;
    unsigned words[CSD_ID_MATERIAL_WORDS];

    if (snap == 0) return 0;

    unsafe {
        uc_audiohw_diag <: (unsigned) AUDIOHW_CMD_IDENTITY;
        uc_audiohw_diag :> ready;
        uc_audiohw_diag :> stage;
        uc_audiohw_diag :> source;
        uc_audiohw_diag :> hi;
        uc_audiohw_diag :> lo;
        for (unsigned k = 0; k < CSD_ID_MATERIAL_WORDS; k++) uc_audiohw_diag :> words[k];
        for (unsigned k = 0; k < CSD_ID_STR_WORDS; k++)      uc_audiohw_diag :> w[k];
    }

    /* The fill is inside unsafe{} because XC rejects dereferencing an unsafe
     * pointer outside one ("cannot dereference unsafe pointer outside of
     * unsafe region") -- the same region already needed for the chanend.
     * Filling from the locals rather than receiving straight into the struct
     * keeps every channel transfer on a bare variable, as elsewhere here. */
    unsafe {
        snap->ready = ready;
        snap->stage = stage;
        snap->source = source;
        snap->hash = ((unsigned long long)hi << 32) | (unsigned long long)lo;
        for (unsigned k = 0; k < CSD_ID_MATERIAL_WORDS; k++) snap->words[k] = words[k];

        /* Unpack 4 chars per word and force termination: a corrupt or truncated
         * reply must yield a short string, never a read past the buffer. */
        for (unsigned k = 0; k < CSD_ID_STR_CAP; k++) {
            unsigned c = (w[k / 4u] >> (8u * (k % 4u))) & 0xFFu;
            snap->str[k] = (char)c;
            if (c == 0u) break;
        }
        snap->str[CSD_ID_STR_CAP - 1u] = 0;
    }
    return 1;
}

int csd_diag_reg_write(unsigned reg, unsigned val)
{
    if (reg > 0xFFu || val > 0xFFu) return 0;

    /* Refused registers, by owner directive 2026-11-05: the DAC's channel
     * mixing is not ours to touch.
     *
     *   0x02 MIXING_N_AUTOMUTE_CONFIG  CH1_MIX_SEL/CH2_MIX_SEL route one I2S
     *                                  slot to BOTH DAC channels. Writing it is
     *                                  how a bench step can silently produce
     *                                  "left input dead, right input on both
     *                                  outputs" while every host control still
     *                                  reads nominal.
     *   0x04 / 0x05 AUTOMUTE_TIME/LEVEL  same register family, same reason, and
     *                                  automute was proven inert for the pop
     *                                  artifact anyway.
     *   0xC0 SOFT_RESET                  not mixing, but it belongs here for
     *                                  the same class of hazard: this build has
     *                                  no codec power-on-reset path (nothing
     *                                  asserts the reset GPIO; csd_AudioHwInit
     *                                  only releases it), so a soft reset that
     *                                  does not come back is unrecoverable
     *                                  without pulling the supply. Measured:
     *                                  one 0xC0 write left the chip at all-zero
     *                                  registers and both outputs silent
     *                                  through two stamp-proven .xe reloads,
     *                                  USBDEVFS_RESET, format changes and a
     *                                  codec re-init.
     *
     * Reads stay open -- refusing them would hide the state we need to see.
     * Return 0 makes the SCPI layer push a hardware error rather than pretend
     * the write landed. */
    if (reg == 0x02u || reg == 0x04u || reg == 0x05u || reg == 0xC0u)
    {
        return 0;
    }

    unsafe {
        uc_audiohw_diag <: (unsigned) AUDIOHW_CMD_REGWR;
        uc_audiohw_diag <: reg;
        uc_audiohw_diag <: val;                   /* no reply on the write path */
    }

    reg_writes++;
    return 1;
}
