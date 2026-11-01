/* L/R trim persistence (TODO item 3, directive 2026-11-01).
 *
 * Two entry points, both plain C, both safe to call from XC: the flash
 * access lives in this translation unit deliberately, the same way
 * csd_gen_mailbox.c puts the cross-thread hand-off words in C -- XC's
 * parallel-usage checker attributes C globals (and the port handles used
 * from C) differently from shared XC statics, and the audio path cannot
 * afford a checker fight over a scratch sector.
 *
 * Storage is COMPILED OUT until CSD_TRIM_FLASH_ADDR is defined by the flash
 * layout audit (see csd_trim_store.c for the brick-risk reasoning). While
 * undefined, the load reports "no valid persistence" -- the documented
 * 0 dB / 0 dB reset default -- and nothing writes flash.
 */
#ifndef __csd_trim_store_h__
#define __csd_trim_store_h__

#ifdef __cplusplus
extern "C" {
#endif

/* Call once at boot, before the first volume application. Adopts the
 * newest VALID record into the mailbox trim word, or leaves it at the
 * 0 dB / 0 dB reset default when the store holds none. */
void csd_trim_load_boot(void);

/* Called by the SCPI trim setter after it updates the mailbox word, from
 * the diagnostic thread (single writer). Journals the current pair. */
void csd_trim_request_save(void);

#ifdef __cplusplus
}
#endif

#endif /* __csd_trim_store_h__ */
