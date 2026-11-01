/* CSD bidirectional diagnostic interface -- device-side entry points.
 *
 * Transport: class-compliant USB MIDI (lib_xua), SCPI text carried inside
 * vendor SysEx. Driverless on Windows/macOS/Linux/iOS; the host talks to it
 * with any MIDI stack, no vendor driver and no WinUSB descriptor.
 *
 * Wire format (must match workspace/csd-diag, which is byte-verified against
 * the vendor parser):
 *   F0  7E  <ASCII command, no CR/LF>  F7
 * A single 0x7E manufacturer id (MMA Educational & Research). Deliberately NOT
 * the tempting three-byte 0x20B1: 0xB1 has bit7 set, and the MIDI rule -- which
 * lib_xua's midi_in_parse() enforces -- is that any byte >= 0x80 inside SysEx
 * is a status byte, so such a message is abandoned mid-frame and the command
 * vanishes with no error anywhere. Every payload byte is 7-bit.
 *
 * Runs on its own logical core (MIDI_TILE). Because it is not the audio path,
 * the register commands may block on the tile-0 I2C owner -- a liberty that
 * would cost the audio stream if taken on the audiohub thread. */
#ifndef __csd_diag_h__
#define __csd_diag_h__

/* The declarations below are shared by C (csd_diag_scpi.c) and XC
 * (csd_diag_midi.xc, csd_diag_regs.xc), so they must NOT be guarded by
 * __XC__ -- guarding them left the C translation unit with no prototypes and
 * the calls resolved as implicit declarations. */

/* The identity snapshot type crosses the tile boundary, so both sides need
 * the same definition. csd_identity.h is written to be includable from C and
 * XC alike (no XC-only keywords outside __XC__ guards). */
#include "csd_identity.h"

#ifdef __XC__
/* MIDI-task replacement (csd_diag_midi.xc), signature-compatible with
 * lib_xua's usb_midi() so main.xc can call either by name. Ports/clock are
 * null-resourced on this board: it has no physical MIDI hardware, the only
 * MIDI entity is the USB endpoint. */
void csd_diag_midi(
    buffered in port:1 ?p_midi_in,
    port ?p_midi_out,
    clock ?clk_midi,
    chanend ?c_midi,
    unsigned cable_number);
#endif /* __XC__ : needs port/clock/chanend types */

/* --- transport <-> parser glue (both on the diagnostic core) ------------ */

/* --- transport <-> parser boundary: SCALARS ONLY -----------------------
 *
 * Both buffers (the command line and the pending reply) live in C, next to
 * the parser that produces and consumes them. The XC transport exchanges one
 * byte at a time and never passes or receives a pointer.
 *
 * That is not shyness about unsafe: XC cannot cast a safe array to an unsafe
 * pointer ("cast to a pointer of a different kind") and cannot qualify an
 * array type unsafe ("unsafe specified on type that is not a pointer or
 * resource"), so a shared buffer means unsafe declarations on both sides plus
 * casts that the compiler rejects outright. Owning each buffer on one side
 * removes the whole class -- and there is nothing to get wrong, because an
 * int argument has no memory space. This is a bench interface carrying tens
 * of bytes per command, so per-byte calls cost nothing measurable.
 *
 * Reply bytes are never silently lost: drops are counted and reported. */

/* Command path (transport -> parser). Feed bytes of one SysEx payload; call
 * csd_scpi_end() at F7 to run the parser. Bytes are ignored unless between
 * start and end, so padding and stray traffic cannot leak into a command. */
void csd_scpi_begin(void);
void csd_scpi_put(unsigned b);
void csd_scpi_end(void);

/* Reply path (parser -> transport), byte-at-a-time pull.
 * csd_scpi_reply_ready() is non-destructive; take() returns 256 when empty so
 * a race can never be mistaken for a 0x00 byte. */
unsigned csd_scpi_reply_ready(void);
unsigned csd_scpi_reply_take(void);

/* Counters, so a partial answer is detectable from the host side. */
void csd_diag_note_reg_write(void);
unsigned csd_diag_reply_drops(void);     /* bytes dropped by the ring */
unsigned csd_diag_reply_truncs(void);    /* replies shortened at REPLY_MAX */
unsigned csd_diag_reg_writes(void);      /* host codec writes through us */

/* --- codec register access (csd_diag_regs.xc) --------------------------- */
/* Forwarded to the tile-0 audio-hw task, the single owner of the codec I2C
 * bus. Both MAY BLOCK on that core. Return 0 on failure; the caller turns a 0
 * into an SCPI hardware error rather than reporting a stale value. */
int csd_diag_reg_read(unsigned reg, unsigned *val);

/* L/R trim persistence proxy (csd_diag_regs.xc): forwards to the tile[0]
 * codec server, which is the only task allowed to drive PORT_SQI_*. See
 * csd_trim_store.h for why they cannot be called directly from this core. */
/* Value travels as a scalar; the CALLER (tile[1]) applies it to the mailbox.
 * See csd_diag_regs.xc for why the store must not publish it itself. */
int csd_diag_trim_load(unsigned *packed_out);
int csd_diag_trim_save(unsigned packed);
unsigned csd_diag_trim_status(unsigned idx);   /* idx 0..6, see csd_trim_store.h */

/* Unit identity, forwarded to the tile[0] owner for the same reason register
 * reads are: the identity statics are per-tile memory (.dp.bss is DP/CP
 * relative), so this core cannot read them directly. Returns 0 on failure; on
 * success *snap is fully populated, including a NUL-terminated snap->str.
 * MAY BLOCK on tile[0].
 *
 * The parameter is an unsafe pointer so the C handlers can pass a local
 * struct's address; `unsafe` is an XC keyword only, hence the guard -- the
 * same reason csd_identity.h guards its pointer return. */
/* `unsafe` qualifies the POINTER and goes AFTER the '*', the way lib_xua
 * declares setSerialStr(char * unsafe serial_str). Putting it before the type
 * means something else entirely and is rejected outright: "unsafe specified
 * on type that is not a pointer or resource". */
#ifdef __XC__
int csd_diag_identity_snapshot(csd_id_snapshot_t * unsafe snap);
#else
int csd_diag_identity_snapshot(csd_id_snapshot_t *snap);
#endif
int csd_diag_reg_write(unsigned reg, unsigned val);

#endif /* __csd_diag_h__ */
