/* csd_power_state -- the cross-TASK state the idle-power feature (TODO item
 * 7, owner directive 2026-11-04) needs between tasks.
 *
 * THE RULE THIS FILE EXISTS TO ENFORCE
 * A C static here is shared only between tasks ON THE SAME TILE. It is NOT a
 * cross-tile communication mechanism. Measured on hardware 2026-11-05 with
 * the first cut of this feature: the mixer (tile[1]) counted 21,925,082 frames
 * into a counter while the codec server (tile[0]) read 0 from the very same
 * variable, and the box powered down on its timeout and then could never wake,
 * because the wake condition was that counter. Each tile gets its own copy, so
 * a cross-tile hand-off here fails SILENTLY: the writer sees no error and the
 * reader sees no value.
 *
 * Consequence for the design: the policy, the stream witness, the DSP bypass
 * and the codec re-init all live on tile[1] (where mixer1, the audio thread
 * and the power assistant already run, so they share), and the tile[0] codec
 * server only executes actuator commands over a channel -- the same shape as
 * the proven DAC_REGWRITE protocol, and the only shape that crosses tiles.
 * csd_vol_mailbox.c is the other sanctioned pattern: ep0 and the server are
 * both on tile[0], so its mailbox works and was bench-proven.
 *
 * Every field below names its tile. Do not move a writer to another tile
 * without converting the hand-off into a channel message.
 *
 * Threading: one writer per word, other tasks read only. A single aligned
 * 32-bit word cannot tear and there is no read-modify-write to race, so plain
 * volatile is sufficient. Anything wider needs a seqlock instead -- reading
 * two of these and expecting them to agree is NOT safe.
 */
#ifndef __csd_power_state_h__
#define __csd_power_state_h__

/* ============================ TILE[1] -------------------------------------
 * Writer: mixer1's GetSamplesFromHost (lib_xua, the raw host word before any
 * volume term). Reader: the power assistant, same tile. Cumulative since
 * boot, so compare against a private cursor rather than expecting a zero. */

/* The buffer path's documented underflow fill. It is NON-ZERO, so a naive
 * 'sample != 0' would keep an idle box awake forever -- the exact failure the
 * burned-week rule warns about. A stuck constant is not a stream, and real DC
 * audio is not this word. */
#define CSD_PW_UNDERFLOW_WORD 0x96969696

void     csd_pw_note_host_sample(unsigned ch, int sample);
unsigned csd_pw_host_activity(void);   /* non-zero (i.e. real) samples seen  */
unsigned csd_pw_host_frames(void);     /* frames received (diagnostics)      */

/* DSP bypass REQUEST, posted by the power assistant, adopted by the audio
 * thread. A LEVEL, not an edge: the audio thread tracks what it last applied,
 * so a request and its cancellation landing between two frames are still
 * corrected rather than stranding the filter off.
 *
 * A flag rather than a call into csd_dsp.xc because an &-to-XC-static made by
 * a called XC function is attributed by the branch checker to the CALLER's par
 * branch, so the assistant touching the engine collided with the audio branch
 * and the image was refused at link. Keep it a flag. */
void     csd_pw_request_dsp_bypass(unsigned enable);
unsigned csd_pw_dsp_bypass_requested(void);

/* Preset held across the bypass, so the restore is exact rather than
 * 'whatever preset 0 is'. Written and read by the audio thread only (it owns
 * the engine); the sentinel makes 'save once' and 'nothing to restore'
 * explicit. */
#define CSD_PW_PRESET_NONE 0xFFFFFFFFu
void     csd_pw_save_preset(unsigned p);
unsigned csd_pw_take_saved_preset(void);

/* Last MCLK the codec was configured for. Written by csd_AudioHwConfig (audio
 * core); read by the power assistant to hand the codec boot script the clock
 * the last real configuration actually used. The assistant cannot call
 * csd_dsp_get_rate() itself: same branch-checker reason as the bypass flag. */
void     csd_pw_note_rate(unsigned mclk);
unsigned csd_pw_last_mclk(void);

/* Boot-path MASTER TRIM cache. Was two function-scope statics inside
 * csd_AudioHwInit(); the power assistant calls that same function from another
 * par branch, and the branch checker treats function-scope statics as shared
 * objects. Same semantics as before: write on the first call, then again only
 * when the dirty flag moves the value. */
unsigned csd_pw_trim_boot_should_write(unsigned q31);

/* ===================== WIRE PROTOCOL (assistant -> server ONLY) -----------
 * The ONE channel this feature uses to cross tiles, and it carries traffic in
 * exactly one direction: the tile[1] power assistant sends actuator commands,
 * the tile[0] codec server receives and executes them.
 *
 * Why only one direction: `chan <: x` blocks until the far end selects to
 * receive. The codec server services this core's I2C traffic, and the codec
 * boot script the assistant runs goes through that service. A reverse send
 * would let each side wait on the other while holding what the other needs --
 * the deadlock the first cut produced. One-way traffic makes that
 * unrepresentable.
 *
 * Ordering: a dedicated chanend preserves message order, so VOL_MIN ->
 * AMP_OFF -> DAC_OFF arrives and executes in that order with no handshake and
 * no acknowledgment. The policy treats an action as complete once sent; the
 * server executes it before returning to its select. Worst case a command
 * lags one server tick, which is noise against a 5-minute timeout.
 *
 * Only these five cross. DSP bypass does NOT: its flag is tile[1] state and
 * the assistant that decides it lives on tile[1], so posting it locally is
 * both correct and cheaper. Neither does the codec re-init: the assistant
 * runs the boot script itself, because it blocks and the server must stay
 * free to service the I2C those writes travel over. GPIO 8D and the codec
 * registers, by contrast, belong to the tile[0] server (its sole-writer
 * rule), so those five must travel.
 *
 * Values start at 0x40 so they cannot collide with AUDIOHW_CMD_* (an enum from
 * 0 in clearsounddac_board.h) should the two servers ever be merged. */
#define CSD_PW_CMD_VOL_MIN      0x40u  /* pop-suppression: level to floor   */
#define CSD_PW_CMD_AMP_OFF      0x41u  /* AMP_EN low                        */
#define CSD_PW_CMD_DAC_OFF      0x42u  /* system mute + hold codec in reset */
#define CSD_PW_CMD_VOL_RESTORE  0x43u  /* saved level back after re-init    */
#define CSD_PW_CMD_AMP_ON       0x44u  /* AMP_EN high -- LAST in the wake   */

#endif /* __csd_power_state_h__ */
