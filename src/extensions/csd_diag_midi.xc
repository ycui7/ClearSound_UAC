// USB-MIDI transport for the SCPI diagnostic interface.
//
// Replaces lib_xua's usb_midi() in this build (MIDI hook in
// lib_xua/src/core/main.xc, gated on CSD_DIAG_MIDI). Signature-compatible,
// because this board has no physical MIDI hardware: p_midi_in/p_midi_out/
// clk_midi arrive as null resources and are never touched. The only MIDI
// entity is the USB endpoint, and the bytes on it are a SCPI console.
//
// WHY THIS SHAPE
// The c_midi handshake is exactly what lib_xua's own usb_midi() speaks, so the
// USB side (src/core/buffer/ep/ep_buffer.xc) needs no changes:
//   host -> us : midi_get_ack_or_data() yields one 32-bit USB-MIDI event
//   us -> host : midi_send_data(event), then wait for its ack before the next
// Sending two words without waiting desynchronises the ep_buffer state machine,
// which surfaces hours later as silently truncated replies, so the vendor
// sequence is followed exactly.
//
// NO BUFFERS CROSS INTO C
// Command bytes go out one at a time (csd_scpi_put) and reply bytes come back
// one at a time (csd_scpi_reply_take); the command buffer and the reply ring
// both live in csd_diag_scpi.c, beside the parser. That is a real constraint,
// not caution: XC will not cast a safe array to an unsafe pointer ("cast to a
// pointer of a different kind") and will not qualify an array type unsafe
// ("unsafe specified on type that is not a pointer or resource"), so sharing a
// buffer means unsafe declarations plus rejected casts on both sides. Each
// side owning its buffer deletes that class of problem, and at a few tens of
// bytes per bench command the per-byte calls cost nothing measurable.
//
// DEADLINE DISCIPLINE
// This core must keep answering the endpoint, so it blocks on nothing but the
// MIDI channel and never prints. The register commands it can trigger do block
// on the tile-0 I2C owner -- legitimate only because this is not the audio
// path. The same behaviour on the audiohub thread cost us the silent-DAC week.

#include <xs1.h>
#include <xclib.h>          // byterev()
#include <stdint.h>
#include <stdio.h>          /* CSD_DIAG_TRACE only: this core is NOT the audio
                            * path, so a print cannot cost an LRCLK deadline.
                            * The audiohub thread must never print -- that is
                            * what cost us the silent-DAC week. */

/* midi_get_ack_or_data() needs external linkage to be usable as a select
 * handler; xua_midi.h grants that only when this is defined (the definition
 * lives in lib_xua/src/midi/usb_midi_support.xc, which defines __MIDI_IMPL). */
#define NO_INLINE_MIDI_SELECT_HANDLER
#include "xua_midi.h"
#include "csd_diag.h"

/* ---- receive: USB-MIDI events -> SCPI bytes ---------------------------- */
/* Manufacturer id is a single 0x7E (MMA Educational & Research). Deliberately
 * NOT the tempting three-byte 0x20B1: 0xB1 has bit7 set, and the MIDI rule --
 * which lib_xua's midi_in_parse() enforces -- is that any byte >= 0x80 inside
 * SysEx is a status byte, so such a message is abandoned mid-frame and the
 * command vanishes with no error anywhere. Every payload byte is 7-bit. */
#define CSD_SYSEX_ID  0x7E

static unsigned sysex_open    = 0;   /* between F0 and F7 */
static unsigned expect_id     = 0;   /* SysEx bytes still to swallow as
                                      * manufacturer id before the text */
static unsigned parse_pending = 0;   /* F7 seen, parser not yet run */

static void rx_event(unsigned event)
{
    unsigned cin = (event >> 24) & 0xF;
    unsigned b0  = (event >> 16) & 0xFF;
    unsigned b1  = (event >> 8)  & 0xFF;
    unsigned b2  = (event >> 0)  & 0xFF;
    unsigned n;

    /* Code index numbers, USB Audio 1.0 Table 4-6 (host->device direction). */
    if      (cin == 0x4) n = 3;                  /* SysEx starts / continues  */
    else if (cin == 0x7) n = 3;                  /* ends, following 3 bytes   */
    else if (cin == 0x5) n = 2;                  /* ends, following 2 bytes   */
    else if (cin == 0x6) n = 1;                  /* ends, following 1 byte    */
    else return;                                 /* channel/common/unused     */

    /* ...but do not TRUST it for a terminal event. The USB Audio spec and the
     * vendor producer (lib_xua/src/midi/midiinparse.xc,
     * `codeIndexNumber = received + 0x4`) disagree for 1- and 2-byte tails.
     * Trusting the field on a 2-byte tail announced as 0x6 would read one
     * byte, never see F7, leave the message open, then absorb the 0x00 padding
     * as command text -- the command would hang rather than fail. So on a
     * terminal event take the length from where F7 actually is, falling back to
     * the CIN only when no F7 appears (malformed). The host client in
     * workspace/csd-diag mirrors this. */
    if (cin != 0x4) {
        for (unsigned k = 0; k < 3; k++) {
            unsigned kb = (k == 0) ? b0 : ((k == 1) ? b1 : b2);
            if (kb == 0xF7) { n = k + 1u; break; }
        }
    }

    for (unsigned i = 0; i < n; i++) {
        unsigned b = (i == 0) ? b0 : ((i == 1) ? b1 : b2);

        if (b == 0xF0) {
            /* A host that starts a message without ending the previous one is
             * out of protocol; complete what we hold first so a stuck
             * half-message cannot swallow the new command. */
            if (parse_pending) { csd_scpi_end(); parse_pending = 0; }
            sysex_open = 1;
            expect_id  = 1;          /* our id is one byte -- see below */
            csd_scpi_begin();
            continue;
        }
        if (!sysex_open) continue;               /* junk before any F0        */

        /* SysEx is F0 <manufacturer-id...> <data> F7. The id is NOT command
         * text. Feeding it in was a real bug, caught on hardware: the command
         * reached the parser as "~*IDN?" (0x7E is '~'), matched no pattern,
         * queued an -113 error and therefore produced NO reply at all -- the
         * host just timed out, which looks like a dead transport rather than a
         * misparsed command. Skip it, and abandon a message that does not
         * carry our id rather than trying to parse someone else's gear. */
        if (expect_id) {
            if (b != CSD_SYSEX_ID) { sysex_open = 0; expect_id = 0; return; }
            expect_id = 0;
            continue;
        }

        if (b == 0xF7) {
            /* Parsing is deferred to the task loop, so a new reply can never
             * be written while a previous one is half sent. Bytes after F7 in
             * this event are zero padding, skipped by the guard above. */
            sysex_open    = 0;
            expect_id     = 0;
            parse_pending = 1;
            continue;
        }
        if (b >= 0x80u) {                        /* status byte mid-frame:
                                                * abandon and resync on the next
                                                * F0 rather than feed the parser
                                                * garbage */
            sysex_open = 0;
            expect_id  = 0;
            continue;
        }
        csd_scpi_put(b);
    }
}

/* ---- send: reply bytes -> USB-MIDI SysEx events ------------------------ */
/* Emit the spec convention (USB Audio 1.0 Table 4-6): CIN 0x4 for full 3-byte
 * chunks, then 0x7 / 0x5 / 0x6 for tails carrying 3 / 2 / 1 data bytes where
 * the last data byte is F7.
 *
 * The host client deliberately accepts BOTH this and the vendor producer
 * convention (which disagrees for 1- and 2-byte tails), because whatever
 * produced the bytes is not ours to control. Send correct, read liberally. */
static unsigned pend[3];
static unsigned npend    = 0;
static unsigned msg_open = 0;                  /* mid-SysEx: F0 already sent */

static unsigned make_event(unsigned cin, unsigned b0, unsigned b1, unsigned b2)
{
    /* BYTE-REVERSED on purpose, and this is the one thing easy to get wrong.
     *
     * ep_buffer hands this word straight to write_via_xc_ptr(), i.e. it lands
     * in the USB buffer little-endian, so the word's LOW byte must be the
     * packet's FIRST byte. The packet layout we want on the wire is
     *     byte0 = (cable<<4)|CIN, byte1..3 = MIDI bytes
     * therefore byte0 has to be the low byte of the word we pass in.
     *
     * The vendor proves it: usb_midi.xc lines 150 and 254 do
     *     event = byterev(event); midi_send_data(c_midi, event);
     * on the device->host leg -- makeEvent() builds the logical layout
     * (cable<<28|CIN<<24|b0<<16|b1<<8|b2) and it is byte-reversed before the
     * channel send. (The host->device leg reverses on the way IN, which is why
     * rx_event() takes byterev(datum) and reads CIN from bits 24-27: that
     * direction is already right here and must not be "fixed" to match this.)
     *
     * Symptom of getting it wrong, which is how this was caught: the device
     * transmits, every event is acked, the trace shows the whole reply going
     * out -- and the host silently drops every packet because it reads a MIDI
     * data byte as the header (0x43 'C' => cable 4, CIN 3 => reserved). */
    return byterev((0u << 28) | (cin << 24) | (b0 << 16) | (b1 << 8) | b2);
}

/* Stage and send one event, if the handshake is idle. */
static void tx_kick(chanend c, int *waiting)
{
    if (*waiting) return;

    if (!msg_open) {
        if (!csd_scpi_reply_ready()) return;         /* nothing to say */
        msg_open = 1;
        npend    = 0;
        pend[npend++] = 0xF0;
        pend[npend++] = CSD_SYSEX_ID;
    }

    while (npend < 3) {
        unsigned b = csd_scpi_reply_take();
        if (b == 256u) break;                    /* 256 = ring empty */
        if (b >= 0x80u) continue;                /* SCPI text is 7-bit; never
                                                * emit a byte a receiver would
                                                * read as a status byte */
        pend[npend++] = b;
    }

    unsigned cin;
    if (npend == 3) {
        cin = 0x4;                                 /* continues */
    } else {
        /* Close the message with F7. Payload bytes in a terminal event,
         * counting F7 itself, are (CIN - 4) -- USB Audio 1.0 Table 4-6:
         *   0x5 = ends, 1 byte  = [F7, --, --]
         *   0x6 = ends, 2 bytes = [d, F7, --]
         *   0x7 = ends, 3 bytes = [d, d, F7]
         * Getting these two the wrong way round is NOT cosmetic. The kernel
         * computes the payload length from the CIN, so a 2-byte tail announced
         * as 0x5 makes it emit one byte and treat the packet as finished: the
         * F7 is dropped, the SysEx is never closed in snd-usb-audio, and the
         * device then looks completely dead to every later query -- only the
         * first reply arrives, minus its terminator. Observed on hardware as
         * rawmidi returning 34 bytes ending '0d 0a' (no F7) once and nothing
         * afterwards, while the firmware log showed the whole reply sent. */
        if      (npend == 2) { pend[2] = 0xF7;                     cin = 0x7; }
        else if (npend == 1) { pend[1] = 0xF7; pend[2] = 0x00;     cin = 0x6; }
        else                 { pend[0] = 0xF7; pend[1] = 0x00; pend[2] = 0x00;
                               cin = 0x5; }
        npend    = 3;
        msg_open = 0;
    }

#ifdef CSD_DIAG_TRACE
    printf("DIAG: tx cin=%u %02x %02x %02x\n", cin, pend[0], pend[1], pend[2]);
#endif
    midi_send_data(c, make_event(cin, pend[0], pend[1], pend[2]));
    /* Reset the staging count: without this the next call finds npend still 3,
     * skips the refill loop entirely, and re-sends the SAME event for every
     * ack -- observed on hardware as an endless "tx cin=4 f0 7e 43" with the
     * reply never completing. The bytes were consumed, so they must be dropped
     * from the staging buffer once handed to the endpoint. */
    npend    = 0;
    *waiting = 1;
}

/* ---- task ------------------------------------------------------------- */
void csd_diag_midi(
#if (MIDI_RX_PORT_WIDTH == 4)
    buffered in port:4 ?p_midi_in,
#else
    buffered in port:1 ?p_midi_in,
#endif
    port ?p_midi_out,
    clock ?clk_midi,
    chanend ?c_midi,
    unsigned cable_number)
{
    int waiting_for_ack = 0;

    /* p_midi_in / p_midi_out / clk_midi are null and unused; they exist so
     * main.xc can call this task where it would call usb_midi(). (No `(void)x;`
     * silencers -- XC rejects a cast of a resource type.) */

    msg_open = 0; npend = 0;
    sysex_open = 0; expect_id = 0; parse_pending = 0;

    while (1) {
        int is_ack = 0;
        unsigned int datum = 0;

        select {
            case midi_get_ack_or_data(c_midi, is_ack, datum):
                break;
        }
#ifdef CSD_DIAG_TRACE
        /* Unconditional: an earlier placement inside the !waiting/!msg_open
         * guard hid every ack, which read like a missing ack. */
        printf("DIAG: sel %s datum=%08x wait=%d open=%d\n",
               is_ack ? "ACK " : "DATA", datum, waiting_for_ack, msg_open);
#endif

        if (is_ack) {
            waiting_for_ack = 0;                   /* our event was accepted */
        } else {
            rx_event(byterev(datum));              /* one event from the host */
            midi_send_ack(c_midi);
        }

        /* Only the ack handshake gates sending.
         *
         * Do NOT gate on !msg_open: msg_open is cleared *inside* tx_kick, so
         * requiring it to be 0 in order to call tx_kick deadlocks the moment a
         * message needs more than one event -- the first event is acked, the
         * guard then excludes tx_kick forever, and the reply stalls at one
         * packet. Observed on hardware: three rx events, one tx, one ack, then
         * silence with wait=1 open=1 in the trace.
         *
         * Parsing is still gated (on msg_open) so a fresh reply cannot be
         * written into the ring while a previous message is half sent. */
        if (!waiting_for_ack) {
            if (parse_pending && !msg_open) {
                parse_pending = 0;
#ifdef CSD_DIAG_TRACE
                printf("DIAG: parse\n");
#endif
                csd_scpi_end();                    /* fills the reply ring */
            }
            tx_kick(c_midi, &waiting_for_ack);
        }
    }
}
