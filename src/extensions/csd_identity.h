/* CSD unit identity: read, hash, report. READ-ONLY BY CONSTRUCTION.
 *
 * Nothing here programs OTP. There is no otp_program* call anywhere in the
 * module and there must never be one: OTP bits burn 0->1 once and cannot be
 * erased, so a mis-provisioned serial is permanent. Provisioning belongs to
 * xburn on the production line, reviewed separately.
 *
 * Two identity sources, reported apart and never conflated:
 *
 *   OTP      board info written by xburn -- the only TRUE per-unit identity.
 *            Read with the same layout lib_otpinfo uses, so a board
 *            provisioned with XMOS tooling reads back here unchanged.
 *   DERIVED  FNV-1a/64 over what the chip reports for free: per-tile
 *            SS_NODE_ID plus the JTAG device id. A board-config FINGERPRINT,
 *            not a serial: SS_NODE_ID comes from the tile configuration and
 *            the JTAG IDCODE names the part type, so two virgin boards of
 *            this design can hash identically. Prefixed D- so it can never
 *            be mistaken for a provisioned O- serial.
 *
 * The all-zeros case is the NORMAL one on an unprovisioned board and is a
 * first-class result, not an error: a header bitmap of 0 is neither "written"
 * (bit30) nor "absent" (bit31), so the header walk stops at its
 * invalid-length guard and the derived identity is used. The raw zeros stay
 * published through csd_identity_raw() so a host can SEE that nothing was
 * provisioned, rather than infer it from an absence.
 *
 * The module is split across two files for one hard reason: the OTP data
 * port must be declared on tile[0] (XC pins port resources to a tile), while
 * the string it produces must reach XUA_Endpoint0_setSerialStr(), whose
 * parameter is an unsafe char*, and XC refuses to pass a safe char array as
 * an unsafe pointer. So csd_identity.xc does the tile-0 reads and the hash
 * and hands SCALARS to csd_identity.c, which owns the strings and the USB
 * call. This is the same boundary csd_diag_midi.xc <-> csd_diag_scpi.c uses:
 * ints across, never pointers. */
#ifndef __csd_identity_h__
#define __csd_identity_h__

/* Where the serial came from, in significance order: NONE < DERIVED < OTP.
 * Anything short of OTP is a bench label, not unit identity -- do not book a
 * warranty or a qualification record against it. */
typedef enum csd_id_source {
    CSD_ID_NONE = 0,      /* nothing readable at all */
    CSD_ID_DERIVED = 1,   /* hash of on-chip registers: NOT guaranteed unique */
    CSD_ID_OTP = 2        /* board-info serial from OTP: true unit identity */
} csd_id_source_t;

/* Hash input, in this exact order, as big-endian 32-bit words. A host
 * recomputing the FNV-1a/64 must use these bytes in this order; changing the
 * order or the width changes every derived serial ever recorded. */
#define CSD_ID_MATERIAL_WORDS 7
/*   [0] tile0 node id      [1] tile1 node id
 *   [2] tile0 jtag id      [3] tile1 jtag id
 *   [4] otp header word (virtual address 0x1fa)
 *   [5] otp serial word    [6] otp board-id word                     */

/* The serial itself: the low 32 bits of the 64-bit identity hash, rendered as
 * exactly this many LOWERCASE hex characters, fixed width. Provenance is not
 * encoded here -- it is a separate query, so this stays pure hex. */
#define CSD_ID_SERIAL_LEN 8

/* Buffer holding the serial (or the pending marker) plus terminator. Both are
 * exactly CSD_ID_SERIAL_LEN characters, so one constant sizes the buffer, the
 * packing and the wire. Deriving them from a single length is what keeps the
 * packing loop from reading past the buffer: an earlier revision left the
 * capacity at 24 after the string shrank to 9, and the word getter walked off
 * the end of it. */
#define CSD_ID_STR_CAP    (CSD_ID_SERIAL_LEN + 1u)
#define CSD_ID_STR_WORDS  ((CSD_ID_STR_CAP + 3u) / 4u)

/* One complete identity, as the tile[0] owner hands it to the tile[1]
 * diagnostic core. A plain struct of scalars: no pointer crosses the tiles,
 * which is the constraint that makes this handoff possible in XC at all. */
typedef struct csd_id_snapshot {
    unsigned ready;                          /* 0 = the read has not completed */
    unsigned stage;                          /* 0..6, see csd_identity_stage() */
    unsigned source;                         /* csd_id_source_t */
    unsigned long long hash;
    unsigned words[CSD_ID_MATERIAL_WORDS];
    char str[CSD_ID_STR_CAP];                /* always NUL-terminated */
} csd_id_snapshot_t;

/* ---- called by the tile[0] boot task (csd_identity.xc) -----------------
 * Single handoff of scalars; the C side does everything else. */
/* How far the staged read got. Necessary rather than convenient: on a
 * virgin board the material words are legitimately all zero, so "all zeros"
 * cannot distinguish "the read completed on a blank OTP" from "the read
 * stalled at the first access". Without this, that is a guess.
 * One stage per hardware access, so a stall names the access that stalled:
 *   0 not started          1 entered, before any access
 *   2 tile0 node id        3 tile0 JTAG device id
 *   4 tile1 node id        5 tile1 JTAG device id
 *   6 OTP consulted (complete) */
void csd_identity_stage(unsigned stage);

void csd_identity_report(unsigned otp_header,
                         unsigned otp_serial,
                         unsigned otp_board_id,
                         unsigned node0, unsigned node1,
                         unsigned jtag0, unsigned jtag1,
                         unsigned long long hash);

/* ---- the read itself (csd_identity.xc) ---------------------------------
 * Its OWN thread, placed on tile[0] in src/core/user_main.h. Not folded into
 * AudioHwRemoteTile0: an earlier revision did that and it wedged the bench,
 * because a blocked OTP read prevented that task from ever reaching the
 * select loop that serves codec register access -- which in turn blocked the
 * single SCPI core and killed the whole diagnostic interface. Read-only
 * code must not be able to take the audio path down. */
void csd_identity_task(void);

/* ---- consumed by the diagnostic core (csd_diag_scpi.c) -----------------
 * All safe to call before the boot task finishes: they report "not yet"
 * rather than blocking or returning a fabricated value. */

/* Provenance of the current serial. */
csd_id_source_t csd_identity_source(void);

/* Progress and material, as SCALARS. The tile-0 server (XC) reads the
 * identity through exactly these, and nothing here takes or returns a
 * pointer: `alias` is part of an XC symbol's type (the C definition carries
 * none, and the linker calls that a mismatch), and an array parameter decays
 * to a pointer in C while XC keeps it an array -- both bit us as link
 * errors. Scalars have no such opinions, and there is nothing to get wrong.
 *
 * The serial STRING is not exported at all: it exists for the USB descriptor
 * only, and csd_identity_usb_publish() does that push inside the C file. The
 * diagnostic path gets the packed string words below and never sees a char*. */

/* 1 once the boot read has completed, 0 before. A blank board is ready with
 * all-zero material; "never ran" is NOT ready. That distinction needs this
 * flag plus the stage, not the material itself. */
unsigned csd_identity_is_ready(void);

/* Material word k (0..CSD_ID_MATERIAL_WORDS-1), 0 if out of range or not
 * ready. Order is the published contract above. */
unsigned csd_identity_material_word(unsigned k);

/* Serial string word k (0..CSD_ID_STR_WORDS-1), 4 packed chars per word,
 * little-endian within the word -- the same packing the tile-to-tile handoff
 * uses, so the formatting rule still lives in exactly one place. */
unsigned csd_identity_str_word(unsigned k);

/* Provenance of the current serial. */
csd_id_source_t csd_identity_source(void);

/* Progress of the staged read, 0..6 (see csd_identity_stage). Lets a bench
 * tell a blank board from a stalled read. */
unsigned csd_identity_get_stage(void);

/* Copy the current serial into lib_xua's USB string table. Must be called
 * from the tile[0] identity thread -- tile[0] owns g_strTable. Called once
 * at thread entry (pending marker, so the descriptor is ours even if a read
 * then blocks) and once after the reads complete (the real serial). */
void csd_identity_usb_publish(void);

/* The 64-bit FNV-1a over the material words. 0 before the read completes. */
unsigned long long csd_identity_hash(void);

#endif /* __csd_identity_h__ */
