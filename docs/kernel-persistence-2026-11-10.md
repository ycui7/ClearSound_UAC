# Kernel persistence — required contract (2026-11-10, owner directive)

Status: **SPECIFIED, NOT IMPLEMENTED.** Dispatch is queued behind the agent currently
holding the bench (384 kHz PRESet 2 / engaged-path investigation). No firmware work has
been done on this yet.

Owner directive, verbatim: *"the kernel need to go to persistent storage. add a command to
explicitly save kernel to persistent storage. do not save to persistence without explicit
command."*

## Rules

1. **Explicit save only.** No coefficient or kernel write, `DIAG:DSP:COEFFicient`,
   `:DEFault`, `PRESet` change, rate change, suspend or wake may touch non-volatile storage.
   Persistence happens ONLY from an explicit host command. Today's behaviour — everything
   volatile, restored to unity on boot — remains correct for every other path, and a test
   must prove it by writing a kernel, power-cycling WITHOUT saving, and reading unity back.

2. **Explicit command.** Add a save command in the existing `DIAG:DSP:` namespace, e.g.
   `DIAG:DSP:COEFFicient:SAVE` (no reply on success, errors through the SCPI error queue,
   consistent with the rest of this device). Counterpart commands needed for the feature to
   be usable and reversible: `:CLEAR` (remove the saved kernel, back to unity default), and
   a readback of persistence state. Do not reuse `:DEFault` for this — `:DEFault` restores
   the in-memory unity kernel and must NOT erase storage.

3. **Prove what it saved.** Persistence must be inspectable: which target, tap count,
   a stored sequence/version number, and a CRC or equivalent. A new `INFO?` field per the
   standing rule (append at the END of the map, never renumber) plus the existing container
   query, so a host can tell "unity because nobody saved" from "unity because I saved unity".

4. **Never boot a corrupt kernel.** On boot, a stored kernel is accepted only if the
   header, target, tap count, width, q-format and length validate exactly — the same
   validator the SCPI path uses. On any validation failure: discard, fall back to the unity
   default, report the discard in the error queue and in the persistence readback. A board
   must never be left running an unverifiable filter because a save was interrupted.

5. **Power-loss and wear safety.** Written so a failure mid-save cannot brick the kernel:
   A/B slots with a sequence number and CRC, newest valid slot wins, older slot still valid
   after a failed write. Sector erase/write must not run on the software-LRCLK bit-timed
   loop — tile[1] has zero margin at 384 kHz — so it belongs on a non-critical context, and
   the measured erase/write duration must be reported with the audio continuity outcome
   during it. Report the storage medium, capacity used, and expected erase-cycle budget.

6. **Establish the medium from the board, not from memory.** Determine what non-volatile
   storage actually exists and what API is already in use (search the app and lib_xua for
   existing journal / settings-persistence / SPI-flash paths) and reuse that mechanism
   rather than inventing a second one. Cite the file and API. If the board has no usable
   non-volatile storage, report that as a blocker with the evidence — do NOT emulate
   persistence in a way that dies on power cycle and call it done, and do NOT overload the
   existing settings journal with large payloads without stating the capacity and wear cost.

7. **Do not disturb the open work.** The `PRESet 2` / engaged-ring data-integrity defect and
   the missing hardware proofs (base64 API battery, ES9219Q volume ramp, ring-underrun vs
   arithmetic split) outrank this. Persistence built on a kernel path that does not yet
   measure as modelled would only persist wrong coefficients.

## Acceptance

- Save a host-composed kernel, power-cycle, and read back the identical kernel with
  persistence state showing it; then measure it as modelled.
- Write a kernel WITHOUT saving, power-cycle, read unity — proving rule 1.
- Interrupt a save (power loss mid-write), power-cycle, board boots with a valid kernel
  (previous or unity) and reports what it discarded.
- `:CLEAR` + power-cycle returns unity.
- Persistence on a target/length that fails validation is refused with an SCPI error and
  changes nothing already stored.
