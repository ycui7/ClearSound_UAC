#!/usr/bin/env python3
"""Bit-perfect soak + latency: host -> USB -> XMOS internal path -> I2S boundary.

The device returns, on its capture stream, the words it was about to put on the
I2S bus (firmware tap, DEBUG:LOOPback). This rig plays a STEREO SIGNAL, reads
that back, asserts frame-for-frame equality against the schedule, and measures
the out->in latency the same markers make visible.

WHY THE STIMULUS IS A SIGNAL AND NOT A CONSTANT
An earlier version used a constant DC word as its positive control. That control
was worthless, and the run that credited it was wrong. A constant carries NO
ORDERING INFORMATION: a replayed, dropped, duplicated or reordered frame is
indistinguishable from a fresh one, so "capture contained exactly the value we
played" is equally consistent with a perfect data path and with a device
replaying one buffer that arrived two seconds ago. It is the same class of error
as a periodic play buffer, which hid a gap of exactly the buffer length, and a
cyclic flash-ID read, which hid a phase.

So both channels carry band-limited noise (csd_signal.noise24_vec) generated as
a pure function of the ABSOLUTE frame index, with known bit patterns injected
every 1024 frames:
  - a real waveform across the field, so the codec's data path sees signal, not
    a constant, on BOTH channels;
  - L and R on independent seeds and uncorrelated, so a channel cross-wire FAILS
    instead of passing;
  - no translational self-similarity at any offset, so a REPLAY of any buffer
    length is detectable -- the property a DC or single-tone stimulus lacks;
  - index-addressable, so one dropped frame costs ONE reported error rather than
    desynchronising checker and device.

NO COUNTER CRUTCH. Nothing in the payload identifies its own frame number, so
ordering claims cannot be smuggled in through a sequence number. Alignment comes
from a sync marker; every defect is classified by resynchronising the CONTENT
against the schedule within a bounded window. Content that cannot be resynchronised
ABORTS the run: an unproved leg is void, never evidence of clean.

LATENCY. Playback is USB Audio ASYNC with a feedback endpoint, so the DEVICE
meters how fast the host may send. The writer is paced to avail_update and never
runs ahead; unpaced it measured 571 k frames/s into a 2000 ms ring, and every
"latency" number from that run was simply our own queue. Each marker is stamped
when the player wrote it and when the reader saw it, with both queue depths, so
the total decomposes into queue vs pipeline instead of hiding one inside the
other.

Read run_xe.sh's comment block before debugging a failure: a wrong image makes
any result meaningless.
"""
import argparse
import base64
import ctypes
import os
import subprocess
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import csd_signal as sig

RATE = sig.RATE
CH = 2
BPB = CH * 4                              # S32_LE subslots
APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
CARD = os.environ.get("CSD_CARD", "3")
# Subdevice per direction; both 0 matches the real device (see open_pcm).
PLAY_DEV = int(os.environ.get("CSD_PLAY_DEV", "0"))
CAP_DEV = int(os.environ.get("CSD_CAP_DEV", "0"))

CHUNK_FRAMES = 32768                      # 85 ms at 384k
# MARKER_PERIOD MUST be an exact multiple of CHUNK_FRAMES. The player emits the
# schedule in CHUNK_FRAMES blocks, so if the period is not a multiple, a marker
# start never lands on a chunk boundary and the writer and reader can never pair
# a marker -- latency would report UNMEASURED for a run that worked fine.
# 768000/32768 = 23.4375 was the first value and exactly that bug.
MARKER_PERIOD = 24 * CHUNK_FRAMES         # 786432 frames = 2.048 s
MARKER_LEN = 4096                         # 10.7 ms: survives a long stall
MIN_RUN = 512
RESYNC_WINDOW = 1 << 15                   # frames searched for a lost match
SEARCH_BLOCK = 8192                       # search granularity (bounded work)
FLIP_MAX_BITS = 6                         # <= this many bits differing from
                                          # the expected entry = corruption,
                                          # not an unexplained frame
# Abort thresholds are PER CHUNK, not cumulative: see Comparator.feed(). The old
# cumulative caps (MAX_RESYNCS=300 / ABORT_AFTER=400) made a device losing ~5
# frames/s abort at ~60 s, so a 30-minute gate could never complete and a 57 s
# run reported a verdict that read as 30 minutes.
MIN_ALIGNED_FRACTION_SHOW = 0.5   # keep in step with align_score
ABORT_PER_CHUNK = 2000     # defects in one CHUNK_FRAMES: comparator runaway
RESYNC_PER_CHUNK = 500     # resyncs in one chunk: schedule tracking lost
MAX_RESYNCS = 300
MAX_REPORT = 20
ABORT_AFTER = 400
STALL_LIMIT = 400


def schedule_vec(idx):
    """(L24, R24) for absolute frame indices, marker frames overlaid.

    ONE definition: the player emits it and the checker expects it. Two copies
    drift, and then the rig compares the device against a stream nobody played
    -- that exact drift once produced a guaranteed false FAIL on every 1024th
    frame. Marker positions are part of the schedule, not an exception to it,
    so player and checker cannot disagree about them either."""
    idx = np.asarray(idx, dtype=np.uint64)
    L, R = sig.expected_vec(idx)
    m = (idx % np.uint64(MARKER_PERIOD)) < np.uint64(MARKER_LEN)
    if m.any():
        L, R = L.copy(), R.copy()
        L[m] = np.uint32(sig.SYNC_L)
        R[m] = np.uint32(sig.SYNC_R)
    return L, R


def pack_frames(L, R):
    """(L24,R24) uint32 -> interleaved S32_LE bytes: 24-bit value in bits
    31..8, low 8 are descriptor padding (bBitResolution is 24)."""
    inter = np.empty((len(L), 2), dtype="<u4")
    inter[:, 0] = L.astype(np.uint32) << np.uint32(8)
    inter[:, 1] = R.astype(np.uint32) << np.uint32(8)
    return inter.tobytes()


def unpack_frames(raw):
    w = np.frombuffer(raw, dtype="<u4")
    n = len(w) // 2
    L = (w[0::2] >> np.uint32(8)).astype(np.int64)
    R = (w[1::2] >> np.uint32(8)).astype(np.int64)
    return L[:n], R[:n]


# ===================== comparator ===========================================
class Comparator:
    """Content-resynchronising checker. A defect's class is EVIDENCE: it is
    decided by searching the schedule for the received content, not assumed.
    Content that cannot be found inside the window ABORTS the run.

      gap    content matches the schedule ahead of us      -> frames lost
      dup    content matches the frame we just had         -> frame repeated
      back   content matches the schedule behind us        -> ring replay
      subst  no index matches, received is zero           -> silence substituted
      flip   no index matches, popcount(xor) small        -> bit corruption
      lost   unresynchronisable                            -> VOID, abort
    """

    def __init__(self):
        self.next_idx = None
        self.prev_idx = None                # index of the last frame consumed:
                                            # an immediate repeat matches THIS,
                                            # which is how 'dup' is told apart
                                            # from a far-back ring replay
        self.total = 0
        self.defects = 0
        self.counts = dict.fromkeys(("gap", "dup", "back", "subst", "flip",
                                      "lost"), 0)
        self.reports = []
        self.xor_hist = {}
        self.max_gap = 0
        self.resyncs = 0
        # Why the run stopped early. Must be printed: an unannounced early exit
        # is how a 57 s run came to be reported as a 30-minute soak.
        self.abort_reason = None
        self.zeros = 0                      # capture fields that were silence

    def anchor(self, idx):
        self.next_idx = int(idx)

    def _search(self, want_l, want_r, centre):
        """Absolute index within +-RESYNC_WINDOW of `centre` whose scheduled
        content equals the received pair, marker frames excluded (a match
        inside a marker run is not evidence of position). Blockwise so a small
        jump is cheap and a large one is bounded. The bound is EXACT: the
        self-test injects a loss just outside it and must be told 'lost', so a
        window that silently reaches further would report an unexplained gap as
        a clean resync."""
        lo0 = max(0, centre - RESYNC_WINDOW)
        hi0 = centre + RESYNC_WINDOW
        best, bestd = None, None
        for lo in range(lo0, hi0, SEARCH_BLOCK):
            hi = min(lo + SEARCH_BLOCK, hi0)
            idx = np.arange(lo, hi, dtype=np.uint64)
            L, R = schedule_vec(idx)
            hit = np.nonzero((L == np.uint32(want_l)) &
                             (R == np.uint32(want_r)))[0]
            if len(hit):
                cand = idx[hit]
                inmark = ((cand % np.uint64(MARKER_PERIOD)) <
                          np.uint64(MARKER_LEN))
                cand = cand[~inmark]
                if len(cand):
                    c = int(cand[np.argmin(np.abs(cand.astype(np.int64)
                                                 - centre))])
                    if bestd is None or abs(c - centre) < bestd:
                        best, bestd = c, abs(c - centre)
        return best, bestd

    def _classify(self, got_l, got_r, expected_idx, pos):
        """Decide what happened to one frame. Order matters:
          1. content found elsewhere in the window  -> ordering defect (gap/dup/
             back). This is EVIDENCE, not an assumption.
          2. content found nowhere, but a few bits differ from the entry we
             expected -> CORRUPTION. Note a bit flip changes the value, so
             search can never find it: classifying a flip as 'lost' (which is
             what an earlier version of this function did, and the fault
             injection self-test caught it) would abort on the very defect this
             rig exists to find.
          3. content found nowhere and unlike the expected entry -> unexplained.
             ABORT. An unproved leg is void, never evidence of clean.
        """
        self.defects += 1
        # resyncs counts ACTUAL resynchronisations only -- the `found is not
        # None` path below, where the schedule index was re-derived by search.
        # It used to be bumped for every classified defect, including subst and
        # flip, which do NOT resync: they resume at expected_idx+1 with no
        # search at all. A fully-explained underflow therefore burned the
        # MAX_RESYNCS/ABORT_AFTER budget, so a device losing ~5 frames/s aborted
        # at ~60 s and the 30-minute bar was unreachable regardless of device
        # quality -- and the run printed a verdict on 57 s while claiming 30 min.
        found, _ = self._search(got_l, got_r, expected_idx)
        eL, eR = schedule_vec(np.array([expected_idx], dtype=np.uint64))
        eL, eR = int(eL[0]), int(eR[0])
        x = (int(got_l) ^ eL) | (int(got_r) ^ eR)
        if x:
            b, bits = 0, 0
            while x:
                if x & 1:
                    self.xor_hist[b] = self.xor_hist.get(b, 0) + 1
                    bits += 1
                x >>= 1
                b += 1
        else:
            bits = 0

        if found is not None:
            jump = found - expected_idx
            # A REPEATED frame is found BEHIND us, at the index we just
            # consumed -- so 'dup' is detected against prev_idx, not by
            # jump == 0, which is unreachable because a matching frame would
            # never have reached classify. (An earlier version tested
            # jump == 0, so duplicates were tallied as 'back'.)
            if found == self.prev_idx:
                kind = "dup"
            elif jump > 0:
                kind = "gap"
            else:
                kind = "back"
            self.counts[kind] += 1
            self.resyncs += 1          # the ONLY true resync
            if jump > 0:
                self.max_gap = max(self.max_gap, jump)
            if len(self.reports) < MAX_REPORT:
                self.reports.append((kind, pos, got_l, got_r, expected_idx,
                                     found))
            self.prev_idx = found
            self.next_idx = found + 1      # resume at the frame's TRUE index
            return
        if got_l == 0 and got_r == 0 and (eL or eR):
            self.counts["subst"] += 1
            if len(self.reports) < MAX_REPORT:
                self.reports.append(("subst", pos, got_l, got_r, expected_idx,
                                     -1))
            self.prev_idx = expected_idx
            self.next_idx = expected_idx + 1
            return
        if bits and bits <= FLIP_MAX_BITS:
            self.counts["flip"] += 1
            if len(self.reports) < MAX_REPORT:
                self.reports.append(("flip", pos, got_l, got_r, expected_idx,
                                     -1))
            self.prev_idx = expected_idx
            self.next_idx = expected_idx + 1
            return
        self.counts["lost"] += 1
        if len(self.reports) < MAX_REPORT:
            self.reports.append(("lost", pos, got_l, got_r, expected_idx, -1))

    def feed(self, Lw, Rw, pos0=0):
        """int64 24-bit arrays against the schedule. Returns True to abort.
        Advances next_idx from the DEVICE's content, never a blind +1 -- that is
        what turns one real drop into a cascade of millions."""
        if self.next_idx is None:
            raise RuntimeError("feed() before anchor()")
        d0, r0 = self.defects, self.resyncs
        n = len(Lw)
        if n == 0:
            return False
        self.zeros += int((Lw == 0).sum()) + int((Rw == 0).sum())
        i = 0
        while i < n:
            # INVARIANT: next_idx is the schedule index of Lw[i]. So want ==
            # next_idx, never next_idx + i -- adding i double-counted the offset
            # after every resync, and failing to advance next_idx on a clean
            # pass made every chunk boundary re-check the schedule from the same
            # index (the self-test reported exactly 4 defects for 4.6 chunks).
            want = self.next_idx
            idx = np.arange(want, want + (n - i), dtype=np.uint64)
            eL, eR = schedule_vec(idx)
            ok = (Lw[i:] == eL.astype(np.int64)) & (Rw[i:] == eR.astype(np.int64))
            bad = np.nonzero(~ok)[0]
            if not len(bad):
                self.prev_idx = want + (n - i) - 1
                self.next_idx = want + (n - i)
                break
            k = int(bad[0])
            # Frames want..want+k-1 all matched, so the frame immediately before
            # the mismatch is want+k-1. Track prev_idx HERE, not only when a
            # clean pass completes -- a defect inside the first chunk would
            # otherwise see prev_idx None and tally a held frame as 'back'.
            self.prev_idx = want + k - 1
            self._classify(Lw[i + k], Rw[i + k], want + k, pos0 + i + k)
            i += k + 1
            if self.counts["lost"]:
                break
        self.total += n
        # Abort on a RATE, not a cumulative count. The abort exists to stop a
        # runaway comparator -- a rig bug where every frame mismatches, which
        # shows up as thousands of defects inside one CHUNK_FRAMES=32768 chunk.
        # A real device underflow is ~5/s, i.e. <1 per chunk, and is already a
        # FAIL by the verdict rule (defects must be 0), so it needs to COMPLETE
        # and report, not abort. With cumulative caps (defects > 400,
        # resyncs > 300) a device losing 5 frames/s aborted at ~60-80 s and the
        # run printed a verdict claiming 30 minutes on 57 s of data.
        d_chunk = self.defects - d0
        r_chunk = self.resyncs - r0
        if self.counts["lost"]:
            self.abort_reason = ("unresynchronisable content (lost) -- an "
                                 "unproved leg is void, never evidence of clean")
        elif d_chunk > ABORT_PER_CHUNK:
            self.abort_reason = (f"{d_chunk} defects inside one {n}-frame chunk "
                                 f"(> {ABORT_PER_CHUNK}) -- comparator runaway")
        elif r_chunk > RESYNC_PER_CHUNK:
            self.abort_reason = (f"{r_chunk} resyncs inside one {n}-frame chunk "
                                 f"(> {RESYNC_PER_CHUNK}) -- schedule tracking "
                                 f"lost")
        return bool(self.abort_reason)


# ===================== fault injection: prove the rig can fail ==============
def rig_self_test():
    def stream(n, i0=0):
        L, R = schedule_vec(np.arange(i0, i0 + n, dtype=np.uint64))
        return L.astype(np.int64), R.astype(np.int64)

    fails = []

    # 1. CLEAN must be silent. A comparator that flags a correct stream is the
    #    failure mode that has cost the most time in this project.
    L, R = stream(150000)
    c = Comparator(); c.anchor(0)
    for s in range(0, 150000, CHUNK_FRAMES):
        c.feed(L[s:s + CHUNK_FRAMES], R[s:s + CHUNK_FRAMES], s)
    if c.defects or any(c.counts.values()):
        fails.append(f"CLEAN stream reported {c.defects} defects {c.counts}")

    # 2. one dropped frame -> exactly one gap, and checking CONTINUES
    L, R = stream(60000)
    keep = np.ones(60000, dtype=bool); keep[10000] = False
    c = Comparator(); c.anchor(0)
    c.feed(L[keep], R[keep], 0)
    if c.counts["gap"] != 1 or c.counts["flip"] or c.counts["subst"]:
        fails.append(f"dropped 1 frame gave {c.counts}")
    if c.total < 55000:
        fails.append(f"rig stopped checking after the drop (total={c.total})")

    # 3. a 5000-frame drop is ONE gap, not 5000 defects
    L, R = stream(80000)
    keep = np.ones(80000, dtype=bool); keep[20000:25000] = False
    c = Comparator(); c.anchor(0)
    c.feed(L[keep], R[keep], 0)
    if c.counts["gap"] != 1 or c.max_gap != 5000:
        fails.append(f"5000-frame drop gave gap={c.counts['gap']} "
                     f"max_gap={c.max_gap} counts={c.counts}")

    # 4. a HELD frame (immediate repeat -- the common stall signature) must be
    #    'dup'. It is told from a far-back ring replay by matching against the
    #    frame just consumed, so a repeat of frame 19999 right after frame 19999
    #    is a dup, while a replay of frame 10000 after 19999 is 'back'.
    L, R = stream(40000)
    c = Comparator(); c.anchor(0)
    c.feed(np.concatenate([L[:20000], L[19999:20000], L[20000:]]),
           np.concatenate([R[:20000], R[19999:20000], R[20000:]]), 0)
    if c.counts["dup"] != 1:
        fails.append(f"held frame gave {c.counts}")

    # 4b. a far-back replay must be 'back', not 'dup': inserting frame 10000
    #     after frame 19999 matches content we consumed long ago.
    L, R = stream(40000)
    c = Comparator(); c.anchor(0)
    c.feed(np.concatenate([L[:20000], L[10000:10001], L[20000:]]),
           np.concatenate([R[:20000], R[10000:10001], R[20000:]]), 0)
    if c.counts["back"] != 1:
        fails.append(f"far-back replay gave {c.counts}")

    # 5. single bit flip -> flip, recorded in the XOR histogram
    L, R = stream(40000)
    R2 = R.copy(); R2[25000] ^= 1 << 7
    c = Comparator(); c.anchor(0)
    c.feed(L, R2, 0)
    if 7 not in c.xor_hist:
        fails.append(f"1-bit flip did not land in xor histogram: {c.counts} "
                     f"{sorted(c.xor_hist)}")

    # 6. silence substituted for signal -> subst. This is the defect class the
    #    user's question is about, so it must be detected, not averaged away.
    L, R = stream(40000)
    R2 = R.copy(); R2[30000:30050] = 0
    c = Comparator(); c.anchor(0)
    c.feed(L, R2, 0)
    if c.counts["subst"] + c.counts["gap"] + c.counts["lost"] < 1:
        fails.append(f"50 zeroed frames gave {c.counts}")
    if c.zeros < 50:
        fails.append(f"zero counter did not see the injected silence: {c.zeros}")

    # 7. RING REPLAY must be LOUD. This is exactly the defect a DC stimulus
    #    hides: content is genuine, so a payload check alone says PASS.
    L, R = stream(40000)
    blk = 4096
    c = Comparator(); c.anchor(0)
    aborted = c.feed(np.concatenate([L[:blk]] * 12),
                     np.concatenate([R[:blk]] * 12), 0)
    if not (c.counts["back"] or c.counts["dup"] or c.counts["lost"] or aborted):
        fails.append(f"a replayed {blk}-frame buffer was SILENT: {c.counts}")

    # 8. loss too large to explain must ABORT -- unproved is void, not clean
    L, R = stream(80000)
    keep = np.ones(80000, dtype=bool)
    keep[10000:10000 + RESYNC_WINDOW + 4000] = False
    c = Comparator(); c.anchor(0)
    if not c.feed(L[keep], R[keep], 0):
        fails.append("an unresynchronisable loss was NOT reported")

    # 9. clean run across a marker boundary must stay silent (this is what
    #    makes the latency measurement trustworthy)
    L, R = stream(MARKER_LEN + 2000, i0=MARKER_PERIOD - 1000)
    c = Comparator(); c.anchor(MARKER_PERIOD - 1000)
    c.feed(L, R, 0)
    if c.defects:
        fails.append(f"marker boundary reported {c.defects} defects {c.counts}")

    # 10. the stimulus must be able to expose a replay at realistic buffer sizes
    a = sig.noise24_vec(np.arange(0, 20000, dtype=np.uint64))
    for k in (1024, 4096, 16384):
        seg = 20000 - k
        if int((a[:seg] == a[k:k + seg]).sum()) > 0.02 * seg:
            fails.append(f"stimulus self-similar at offset {k}")

    # 11. STIMULUS IS NOT A CONSTANT: the property the user demanded. A constant
    #     would satisfy replay-detection for every offset; noise must not.
    if len(np.unique(a)) < 0.5 * len(a):
        fails.append("stimulus is near-constant -- it cannot expose ordering "
                     "defects")
    if float(np.std(a.astype(np.float64))) < (1 << 20):
        fails.append("stimulus amplitude too small to exercise the field")

    for f in fails:
        print(f"RIG-BUG: {f}")
    print("RIG-SELFTEST:" + ("PASS" if not fails else f"FAIL ({len(fails)})"))
    return not fails


# ===================== ALSA =================================================
alsa = ctypes.CDLL("libasound.so.2")
for _f, _r, _a in (("snd_pcm_format_value", ctypes.c_int, [ctypes.c_char_p]),
                   ("snd_pcm_format_name", ctypes.c_char_p, [ctypes.c_int]),
                   ("snd_strerror", ctypes.c_char_p, [ctypes.c_int]),
                   ("snd_pcm_state", ctypes.c_int, [ctypes.c_void_p]),
                   ("snd_pcm_avail_update", ctypes.c_long, [ctypes.c_void_p]),
                   ("snd_pcm_writei", ctypes.c_long,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
                   ("snd_pcm_readi", ctypes.c_long,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong])):
    getattr(alsa, _f).restype = _r
    getattr(alsa, _f).argtypes = _a


def _resolve_format(want):
    """Resolve via ALSA's own table, verifying both directions. An early version
    labelled enums from a hand-typed dict and called enum 10 'S24_4LE' (ALSA
    calls it S32_LE); acting on that retargeted the rig to a format that does
    not exist here. A rig that validates an unintended container still prints
    PASS, so this raises instead of guessing."""
    for v in range(0, 64):
        nm = alsa.snd_pcm_format_name(v)
        if nm is None:
            continue
        nm_b = nm if isinstance(nm, bytes) else nm.encode()
        if nm_b.decode().upper() == want.upper():
            if alsa.snd_pcm_format_value(nm_b) != v:
                raise SystemExit(f"format resolver disagrees for {want}")
            return v
    raise SystemExit(f"ALSA has no format named {want!r}; refusing to guess")


WIRE_FORMAT = _resolve_format("S32_LE")
ACCESS_RW_INTERLEAVED = 3                   # probed: MMAP(0), RW_INTERLEAVED(3)


def die(msg, rc=1):
    print(f"FAIL: {msg}")
    sys.exit(rc)


def scpi(cmd):
    r = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                        "--timeout", "15", cmd], cwd=APP,
                       capture_output=True, text=True, timeout=40)
    return (r.stdout or "").strip().replace("\r", "")


def require(cmd, want=None, what=""):
    got = scpi(cmd)
    if want is not None and want not in got:
        die(f"{what}: asked {cmd!r} wanted {want!r} got {got!r}")
    return got


def open_pcm(direction, label):
    """hw:CARD,0 forced to S32_LE @384k with the negotiation VERIFIED. The
    stream number is 0 in BOTH directions (pcm0p/pcm0c); passing `direction` as
    the number asks for hw:3,1 and fails in a way that reads exactly like a
    device that cannot capture."""
    # Device number is parameterised for the FIXTURE TEST. The device puts
    # playback and capture on the SAME subdevice 0, which is why 0 was hardcoded
    # -- but the kernel loopback card (snd-aloop) puts playback on pcm0p and
    # capture on pcm1c, so the identical rig code cannot address it without
    # this. Set CSD_PLAY_DEV/CSD_CAP_DEV to prove the rig, the host ALSA path and
    # the comparator with no USB and no firmware in the loop.
    dev = CAP_DEV if direction else PLAY_DEV
    pcm = ctypes.c_void_p()
    if alsa.snd_pcm_open(ctypes.byref(pcm), f"hw:{CARD},{dev}".encode(),
                         direction, 0) < 0:
        die(f"cannot open {'capture' if direction else 'playback'} "
            f"hw:{CARD},{dev}")
    hp = ctypes.c_void_p()
    alsa.snd_pcm_hw_params_malloc(ctypes.byref(hp))
    alsa.snd_pcm_hw_params_any(pcm, hp)
    for fn, arg in ((alsa.snd_pcm_hw_params_set_access, ACCESS_RW_INTERLEAVED),
                    (alsa.snd_pcm_hw_params_set_format, WIRE_FORMAT),
                    (alsa.snd_pcm_hw_params_set_channels, CH)):
        if fn(pcm, hp, arg) < 0:
            die(f"{label} rejected {fn.__name__} -- enumerate the descriptor, "
                f"do not assume")
    d = ctypes.c_uint(RATE)
    alsa.snd_pcm_hw_params_set_rate_near(pcm, hp, ctypes.byref(d), 0)
    if d.value != RATE:
        die(f"{label} negotiated rate {d.value}, wanted {RATE}")
    if alsa.snd_pcm_hw_params(pcm, hp) < 0:
        die(f"{label} hw_params apply failed")
    got = ctypes.c_int(-1)
    alsa.snd_pcm_hw_params_get_format(hp, ctypes.byref(got))
    if got.value != WIRE_FORMAT:
        die(f"{label} negotiated {alsa.snd_pcm_format_name(got.value)} not "
            f"S32_LE -- refusing to validate the wrong wire format")
    per = ctypes.c_uint(0)
    buf = ctypes.c_uint(0)
    alsa.snd_pcm_hw_params_get_period_size(hp, ctypes.byref(per), 0)
    alsa.snd_pcm_hw_params_get_buffer_size(hp, ctypes.byref(buf))
    alsa.snd_pcm_prepare(pcm)
    return pcm, per.value, buf.value


def queue_frames(pcm, buf_frames, is_play):
    """Frames HELD in the ring, i.e. audio not yet seen by the other side.

    Direction matters and getting it wrong inverts the answer:
      playback -- snd_pcm_avail_update() returns FREE space, so held = size-av.
      capture  -- it returns frames AVAILABLE TO READ, which is exactly what is
                  held, so held = av.
    Treating capture as if it were playback reported the capture ring as ~full
    (767031 of 768000) on a reader that drains it every 32768 frames -- a
    backlog that cannot exist because we read synchronously. Both queue figures
    were then ~2000 ms and the printed decomposition summed to more than the
    total latency, which is what exposed this.
    """
    av = alsa.snd_pcm_avail_update(pcm)
    if av < 0:
        return -1
    return max(0, buf_frames - av) if is_play else min(av, buf_frames)


def write_paced(pcm, data, stats):
    """data must be bytes (a sliced memoryview will not convert to c_void_p).
    PACED: this endpoint is ASYNC with a feedback EP, so the device meters us.
    Running ahead builds a backlog that turns a phase boundary into a measurement
    of audio produced seconds earlier and a latency figure into our own queue."""
    off = 0
    while off < len(data):
        av = alsa.snd_pcm_avail_update(pcm)
        if av < 0:
            stats["recovers"] += 1
            alsa.snd_pcm_recover(pcm, av, 1)
            alsa.snd_pcm_start(pcm)
            continue
        if av == 0:
            stats["paced"] += 1
            time.sleep(0.0005)
            continue
        room = min(len(data) - off, av * BPB)
        w = alsa.snd_pcm_writei(pcm, data[off:off + room], room // BPB)
        if w < 0:
            stats["recovers"] += 1
            if stats["recovers"] > 200:
                return False
            alsa.snd_pcm_recover(pcm, w, 1)
            continue
        off += w * BPB
        stats["sent"] += w
    return True


# ===================== player ===============================================
def player(pcm, stop, release, started, stats, minutes):
    """Emit the schedule paced to the device, HOLDING the first marker until
    capture is watching. Emitting it once at t=0 and starting capture 0.5 s
    later means 192 000 frames have gone by -- the rig then times out blaming
    the device for the rig's own race."""
    alsa.snd_pcm_start(pcm)
    # Signal BEFORE the hold loop. Setting this only after the hold is a
    # deadlock: the hold waits for `release`, and main sets `release` only after
    # started.wait() -- so the rig reported "player never started". "started"
    # means the player is emitting, which is true here, not "the marker went".
    started.set()
    # HOLD: emit the marker block (schedule 0..MARKER_LEN-1) repeatedly until
    # capture is reading. Emitting it once at t=0 and starting capture 0.5 s
    # later means 192 000 frames have gone by -- the rig then times out blaming
    # the device for the rig's own race.
    #
    # Because this block is REPEATED, no frame emitted during the hold may be
    # fed to the comparator: a repeated marker followed by the same tail is not
    # the schedule, and the comparator would report a replay that never happened
    # in the steady-state stream. main() therefore purges the capture ring at
    # release and anchors on the FIRST marker seen afterwards.
    mk = np.arange(0, CHUNK_FRAMES, dtype=np.uint64)
    L, R = schedule_vec(mk)
    mk_bytes = pack_frames(L, R)
    for _ in range(6000):
        if stop.is_set():
            return
        if not write_paced(pcm, mk_bytes, stats):
            return
        stats["sync_sent"] += CHUNK_FRAMES
        if release.is_set():
            break
    stats["sync_done"] = stats["sync_sent"]

    # Continue the schedule where the held block ended, so the stream stays
    # continuous and MARKER_PERIOD being a multiple of CHUNK_FRAMES keeps every
    # later marker on a chunk boundary.
    n = CHUNK_FRAMES
    t0 = time.monotonic()
    while not stop.is_set() and (time.monotonic() - t0) < minutes * 60 + 5:
        idx = np.arange(n, n + CHUNK_FRAMES, dtype=np.uint64)
        L, R = schedule_vec(idx)
        m0 = np.nonzero((idx % np.uint64(MARKER_PERIOD)) == 0)[0]
        # write side: pcm is the playback handle -> avail is FREE space
        q = queue_frames(pcm, stats["play_buf"], True)
        tw = time.monotonic()
        if not write_paced(pcm, pack_frames(L, R), stats):
            return
        if len(m0):
            stats["mark_write"].append((int(idx[m0[0]] // MARKER_PERIOD), tw, q))
        n += CHUNK_FRAMES
    stats["play_seconds"] = time.monotonic() - t0


def find_marker_run(L, R, carry):
    """(index_after_run, run_length) or (None, run_length). The entry test
    matters: if the previous chunk ended inside the marker with the run already
    long enough, this chunk's first non-marker frame IS the anchor."""
    run = carry
    n = len(L)
    if n == 0:
        return None, run
    is_m = (L == sig.SYNC_L) & (R == sig.SYNC_R)
    if run >= MIN_RUN and not is_m[0]:
        return 0, run
    i = 0
    while i < n:
        if is_m[i]:
            j = i
            while j < n and is_m[j]:
                j += 1
            run += j - i
            i = j
            if run >= MIN_RUN:
                return j, run
        else:
            run = 0
            i += 1
    return None, run


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--rig-selftest", action="store_true")
    ap.add_argument("--skip-arm", action="store_true")
    # See main(): drives the same code against snd-aloop to prove the fixture.
    ap.add_argument("--fixture", action="store_true",
                    help="run against the loopback card: proves rig/host, "
                    "not the device")
    # Establish latency -> align globally -> discard outside the aligned region
    # -> only then count. See rig/align_score.py for why counting before
    # aligning is what made rig artifacts look like device defects.
    ap.add_argument("--align-score", action="store_true",
                    help="collect, then latency/align/discard/count (no "
                         "incremental re-anchoring)")
    # Rig-fault discriminator. The reader thread does numpy analysis (unpack +
    # comparator + resync search) while the writer thread feeds a hard real-time
    # ISO endpoint; they share one GIL, so an analysis spike can starve the writer
    # and the DEVICE then underflows -- which would score as device defects that the
    # rig manufactured. The skill prescribes separating collection from analysis
    # for exactly this. --collect-only reads into memory with no analysis, then
    # analyses after the window: if hole counts drop, the defects are rig-induced.
    ap.add_argument("--collect-only", action="store_true",
                    help="collect raw chunks with no analysis, then analyse")
    a = ap.parse_args()

    if a.rig_selftest:
        sys.exit(0 if rig_self_test() else 1)

    if not sig.self_test():
        die("stimulus self-test failed -- the signal cannot expose a replay")

    # --fixture: run the identical rig against the kernel loopback card
    # (snd-aloop) with no USB and no firmware in the path. If the ~5 holes/s
    # appear there too, the FAULT IS THE FIXTURE -- the host ALSA path, the
    # pacing, the reader thread -- and no firmware conclusion can stand until
    # it is fixed. Identity cannot be proven with no device, so say so loudly
    # rather than letting the IDN gate pass silently.
    if a.fixture:
        print(f"!!! FIXTURE TEST -- card {CARD} play_dev {PLAY_DEV} "
              f"cap_dev {CAP_DEV}. NO DEVICE, NO USB, NO FIRMWARE.")
        print("!!! Any defects here are RIG/host faults, not the device's.")

    print("=== arming (every step verified by read-back) ===")
    if a.fixture:
        print("identity: NOT CHECKED (no device in a fixture run)")
    else:
        require("*IDN?", "ClearSound", "identity")
    if not a.skip_arm and not a.fixture:
        require("VOLume:TRIM:LEFT 0", what="trim L")
        require("VOLume:TRIM:RIGHT 0", what="trim R")
        require("VOLume:TRIM?", "0,0",
                "a non-zero trim means we validate an offset DAC")
        # The DSP FIR REWRITES every sample (csd_dsp.xc:187 returns early only
        # for preset 0), so a bit-exact comparison at any other preset is
        # unfalsifiable -- we would be comparing noise against a filtered
        # version of itself. Pin it and read it back; the boot default is
        # CSD_DSP_PRESET_DEFAULT=2 in every build we ship.
        require("DIAG:DSP:PRESet 0", what="DSP preset set")
        got = scpi("DIAG:DSP:PRESet?")
        if got.split(",")[0].strip() != "0":
            die(f"DSP preset is {got!r}, not 0 -- the FIR rewrites every "
                f"sample, so the device would not be bit-exact with itself "
                f"by construction and the run would be unfalsifiable")
        # Likewise the mixer scales the host stream. MIXer:GAIN? answers
        # "w_host,w_gen" in Q15 with 32768 = unity (csd_diag_scpi.c:1106);
        # anything else means the words we compare were multiplied before
        # reaching I2S. w_gen is irrelevant here -- the generator is OFF below.
        #
        # With MIXER=0 there is no mixer task, so the row does not exist and
        # nothing scales the stream in the digital domain -- skipping is correct,
        # but it must be announced, because a silently-skipped gain gate is how a
        # scaled stream would sneak past a "bit-perfect" verdict. The codec-side
        # readback below (regs 0x0F/0x10 = 0 dB, 0x07 unmuted, 0x03 analog max) is
        # the gate that still applies, and it stays mandatory either way.
        got = scpi("MIXer:GAIN?")
        # NOTE: this line read `not got.lower().startswith(("err", ""))` until
        # 2026-11-04. "" is a prefix of EVERY string, so the condition was
        # always False and the unity-gain gate NEVER RAN -- every run, MIXER=1
        # builds included, printed "NO MIXER IN THIS BUILD". A gate that
        # silently skips is worse than no gate: it reads as verified, and it
        # was accidentally correct on MIXER=0 builds, which is why it survived.
        # A missing row is now detected by an actual error/empty reply.
        missing = (not got) or got.lower().startswith(
            ("err", "undefined", "unknown"))
        if not missing:
            require("MIXer:GAIN:HOST 32768", what="mixer host gain to unity")
            got = scpi("MIXer:GAIN?")
            if got.split(",")[0].strip() != "32768":
                die(f"mixer host gain is {got!r}, not Q15 unity 32768 -- the "
                    f"stream reaching I2S is scaled, so it cannot match what "
                    f"we sent")
            print(f"  mixer host gain = unity ({got})")
        else:
            print("  mixer host gain: NO MIXER IN THIS BUILD -- nothing scales "
                  "the stream digitally.\n  Codec 0 dB / unmuted readback below "
                  "is the remaining (mandatory) gate.")
        # REGister:READ? answers base64. (reg, mask, why): reg 3's volume field
        # is bits [4:0] only; the upper bits are unrelated status, so masking
        # the whole register fails a correctly-configured codec (0x03 legitimately
        # reads 0x40). An unchecked armed state is how a mixer build once
        # validated a muted DAC.
        for reg, mask, why in ((0x07, 0x00000001, "system mute off (bit0)"),
                               (0x0F, 0x00FFFFFF, "digital vol L = 0 dB"),
                               (0x10, 0x00FFFFFF, "digital vol R = 0 dB"),
                               (0x03, 0x0000001F, "analog vol field = max")):
            got = scpi(f"REGister:READ? {reg:#04x}")
            try:
                val = int.from_bytes(base64.b64decode(
                    got + "=" * (-len(got) % 4)), "big")
            except Exception:
                val = None
            if val is None or (val & mask):
                die(f"reg {reg:#04x} = {got!r} ({val}); masked field must be 0 "
                    f"-- {why}. Refusing to measure an attenuated or muted path.")
            print(f"  reg {reg:#04x} -> {val:#010x} & {mask:#010x} = 0 ({why})")
        require("GENerator:STATe OFF", what="function generator off")
        require("DEBUG:LOOPback 1", what="tap arm")
    require("DEBUG:LOOPback:CLEAR", what="tap counter reset")
    print("tap armed:", scpi("DEBUG:LOOPback?"))

    play, play_per, play_buf = open_pcm(0, "playback")
    cap, cap_per, cap_buf = open_pcm(1, "capture")
    print(f"periods: play={play_per} capture={cap_per}; rings {play_buf}/"
          f"{cap_buf} frames ({play_buf/RATE*1000:.0f} ms)")

    stop = threading.Event()
    release = threading.Event()
    started = threading.Event()
    stats = {"sent": 0, "recovers": 0, "paced": 0, "sync_sent": 0,
             "sync_done": 0, "play_seconds": 0.0, "mark_write": [],
             "play_buf": play_buf}
    th = threading.Thread(target=player,
                          args=(play, stop, release, started, stats, a.minutes),
                          name="player", daemon=True)
    th.start()
    if not started.wait(40):
        stop.set()
        die("player never started")
    alsa.snd_pcm_start(cap)
    release.set()

    cmp = Comparator()
    buf = (ctypes.c_char * (CHUNK_FRAMES * BPB))()
    anchored = False
    run = 0
    reads = stalls = 0
    latency = []
    collected = []          # (raw bytes, read timestamp) when --collect-only
    t_start = time.monotonic()

    print("\n=== waiting for the sync marker (player holds it) ===")
    while time.monotonic() - t_start < 90:
        n = alsa.snd_pcm_readi(cap, buf, CHUNK_FRAMES)
        if n <= 0:
            stalls += 1
            alsa.snd_pcm_recover(cap, n if n < 0 else -1, 1)
            alsa.snd_pcm_start(cap)
            if stalls > STALL_LIMIT:
                stop.set()
                die(f"capture stalled ({stalls} bad reads): the tap is not "
                    f"driving capture, so we would compare the real ADC against "
                    f"our schedule")
            continue
        reads += 1
        raw = bytes(buf[:n * BPB])
        L, R = unpack_frames(raw)
        after, run = find_marker_run(L, R, run)
        if after is not None:
            # The held block is schedule indices 0..CHUNK_FRAMES-1, of which
            # 0..MARKER_LEN-1 are the marker. So the first frame after the run
            # is schedule index MARKER_LEN -- anchoring at 0 instead made the
            # comparator search, find the content 4096 frames ahead, and report
            # a spurious 4096-frame gap on a run with no defect.
            cmp.anchor(MARKER_LEN)
            anchored = True
            print(f"marker found after {reads} reads; anchored at schedule "
                  f"frame 0")
            if after < n:
                cmp.feed(L[after:], R[after:], pos0=after)
            break
    if not anchored:
        stop.set()
        die(f"no sync marker in 90 s -- the tap is not driving capture or "
            f"playback never started. stats={ {k: v for k, v in stats.items() if k != 'mark_write'} }")

    print(f"=== soaking {a.minutes:g} min @ {RATE} Hz; stereo band-limited "
          f"noise, S32_LE container / 24-bit payload ===")
    # Duration is bounded by the HOST clock, twice over. It must never depend on
    # cmp.total, the count of CAPTURED frames: capture is exactly what the 384 kHz
    # deficit starves (fill ~0.63), so a budget expressed in captured frames
    # retires early by the size of the deficit and, because the sync phase below
    # already consumed one full chunk before the loop was ever entered, a
    # --minutes 1 run previously ended after ~1 s and still printed PASS. A soak
    # whose length is set by the thing under test cannot bound that thing.
    t_soak = time.monotonic()
    deadline = t_soak + a.minutes * 60
    min_frames = RATE * a.minutes          # honest floor, reported, never a gate
    last = t_soak
    while True:
        if time.monotonic() >= deadline:
            break
        if stop.is_set():
            break
        n = alsa.snd_pcm_readi(cap, buf, CHUNK_FRAMES)
        if n <= 0:
            stalls += 1
            alsa.snd_pcm_recover(cap, n if n < 0 else -1, 1)
            alsa.snd_pcm_start(cap)
            if stalls > STALL_LIMIT:
                stop.set()
                die(f"capture stalled after {cmp.total} frames")
            continue
        stalls = 0
        reads += 1
        raw = bytes(buf[:n * BPB])
        tr = time.monotonic()
        if a.collect_only or a.align_score:
            # Collector thread does NOTHING but copy: no unpack, no comparator,
            # no resync search. Those are numpy and share the GIL with the writer
            # feeding a hard real-time ISO endpoint.
            collected.append((raw, tr))
            continue
        L, R = unpack_frames(raw)
        abort = cmp.feed(L, R, pos0=cmp.total)
        # Pair markers on the SCHEDULE index, not on the count of captured
        # frames. The writer stamps (blk, ...) using schedule indices; deriving
        # the reader's block from cmp.total only agrees while capture is full,
        # and at 384 kHz fill ~0.63 it drifts by the deficit -- so chunks never
        # landed on a marker boundary and latency reported UNMEASURED on a run
        # that was otherwise clean. cmp.next_idx is the schedule position just
        # past this chunk, which is the same currency the writer used.
        end_idx = cmp.next_idx
        start_idx = end_idx - n
        m0 = (np.arange(start_idx, end_idx, dtype=np.uint64)
              % np.uint64(MARKER_PERIOD))
        # Find the marker ANYWHERE in the chunk. Requiring it at position 0
        # demanded that reader chunk boundaries align with schedule multiples of
        # MARKER_PERIOD, but the comparator is anchored at MARKER_LEN (4096), so
        # boundaries sit at 4096 + k*32768 and the condition was 1 + 8k = 192m:
        # odd equals even, unsatisfiable. Latency therefore reported UNMEASURED
        # on a run that was clean for 30 minutes -- a rig bug masquerading as a
        # missing measurement, and exactly the failure this file's own header
        # warns about (an unpaced writer once reported UNMEASURED too).
        hit = np.nonzero(m0 == 0)[0]
        if len(hit):
            blk = int((start_idx + int(hit[0])) // MARKER_PERIOD)
            for (b, tw, q) in stats["mark_write"]:
                if b == blk:
                    # read side: cap is the capture handle -> avail
                    # IS the backlog, not the free space
                    latency.append((b, tr - tw, q,
                                    queue_frames(cap, cap_buf, False)))
                    break
        if time.monotonic() - last > 20:
            print(f"  {cmp.total/RATE:6.1f}s checked  defects={cmp.defects} "
                  f"resyncs={cmp.resyncs} zeros={cmp.zeros}", flush=True)
            last = time.monotonic()
        if abort:
            stop.set()
            break

    stop.set()
    th.join(timeout=5)
    alsa.snd_pcm_drop(play)
    alsa.snd_pcm_drop(cap)

    if a.align_score:
        # The disciplined order: latency, then ONE global alignment, then
        # discard everything outside it, then count. No incremental re-anchoring,
        # because re-anchoring is how rig artifacts become device defects.
        import align_score as A
        print(f"=== aligning {len(collected):,} collected chunks "
              f"({len(collected)*CHUNK_FRAMES/RATE:.1f}s) post-stream ===")
        ntot = len(collected) * CHUNK_FRAMES
        cL = np.empty(ntot, dtype=np.int64)
        cR = np.empty(ntot, dtype=np.int64)
        off = 0
        for raw, _tr in collected:
            L, R = unpack_frames(raw)
            k = len(L)
            cL[off:off + k] = L
            cR[off:off + k] = R
            off += k
        collected.clear()
        # centre guess: the writer runs ahead of the device by roughly one ring.
        al = A.align(cL[:off], cR[:off], schedule_vec, sig.SYNC_L, sig.SYNC_R,
                     RATE, centre_guess=stats.get("play_buf", 768000),
                     window=MARKER_PERIOD * 4, marker_period=MARKER_PERIOD)
        print(f"alignment: {al['verdict']}", end="")
        if al["verdict"] != "ALIGNED":
            print(f" -- {al['reason']}")
            print("RESULT: VOID (unaligned -- no error rate is reported)")
            sys.exit(2)
        print()
        for k in ("markers", "spacing", "latency_frames", "latency_ms",
                  "delta", "latency_frames_out_to_in", "latency_ms_out_to_in",
                  "head_discarded", "tail_discarded", "aligned_frames",
                  "aligned_fraction", "n_captured"):
            print(f"  {k:26s} {al.get(k)}")
        sc = A.score(cL[:off], cR[:off], schedule_vec, al["head_discarded"],
                     al["head_discarded"] + al["aligned_frames"], al["delta"])
        print("\n=== RESULT (aligned region only) ===")
        print(f"frames checked : {sc['checked']:,} of {off:,} captured "
              f"({100.0*sc['checked']/max(1,off):.1f}%)")
        print(f"defects        : {sc['defects']}  "
              f"rate={sc['defects']/max(1,sc['checked']):.3e}")
        print(f"zeros (fields) : {sc['zeros']:,}")
        print(f"run histogram  : {sc['run_histogram']}")
        for (p, gl, gr, el, er) in sc["reports"][:10]:
            print(f"  pos={p} got=0x{gl:06X},0x{gr:06X} "
                  f"want=0x{el:06X},0x{er:06X}")
        verdict = (sc["defects"] == 0
                   and al["aligned_fraction"] >= MIN_ALIGNED_FRACTION_SHOW)
        print("\nBIT-PERFECT: " + ("PASS" if verdict else "FAIL"))
        sys.exit(0 if verdict else 1)

    if a.collect_only:
        # Analysis happens AFTER the stream is closed, so no numpy work ever
        # competes with the writer thread. Comparing this run's defect counts
        # against the inline-analysis run is the discriminator: fewer holes here
        # means the holes were manufactured by the rig starving its own writer.
        print(f"=== analysing {len(collected):,} collected chunks "
              f"({len(collected)*CHUNK_FRAMES/RATE:.1f}s) post-stream ===")
        for raw, tr in collected:
            L, R = unpack_frames(raw)
            cmp.feed(L, R, pos0=cmp.total)
            end_idx = cmp.next_idx
            start_idx = end_idx - len(L)
            m0 = (np.arange(start_idx, end_idx, dtype=np.uint64)
                  % np.uint64(MARKER_PERIOD))
            hit = np.nonzero(m0 == 0)[0]
            if len(hit):
                blk = int((start_idx + int(hit[0])) // MARKER_PERIOD)
                for (b, tw, q) in stats["mark_write"]:
                    if b == blk:
                        latency.append((b, tr - tw, q, -1))
                        break
        collected.clear()

    elapsed = time.monotonic() - t_soak
    print("\n=== RESULT ===")
    print(f"soak wall time : {elapsed:.1f}s of {a.minutes*60:.0f}s requested "
          f"(budget is the host clock; captured frames are NOT the clock)")
    print(f"frames checked : {cmp.total:,}  (schedule advanced to "
          f"{cmp.next_idx:,})")
    print(f"defects        : {cmp.defects}  counts={cmp.counts}")
    print(f"resyncs        : {cmp.resyncs}   max_gap={cmp.max_gap}")
    print(f"silence fields : {cmp.zeros:,} = "
          f"{cmp.zeros/max(1,2*cmp.total):.4f} of all capture fields")
    print(f"reads={reads} stalls={stalls} recovers={stats['recovers']} "
          f"paced={stats['paced']}")
    if cmp.reports:
        print("first defects (got vs the schedule entry we expected):")
        for kind, pos, gl, gr, ei, fi in cmp.reports[:MAX_REPORT]:
            eL, eR = schedule_vec(np.array([ei], dtype=np.uint64))
            print(f"  {kind:5s} pos={pos:8d} got=0x{gl:06X},0x{gr:06X}  "
                  f"want=0x{int(eL[0]):06X},0x{int(eR[0]):06X} "
                  f"idx={ei}->matched={fi}")
    if cmp.xor_hist:
        print("xor bit histogram:",
              sorted(cmp.xor_hist.items(), key=lambda kv: -kv[1])[:12])

    print("\n=== LATENCY out->in (host wrote marker -> host read it) ===")
    if latency:
        d = np.array([x[1] for x in latency])
        q = np.array([x[2] for x in latency], dtype=np.float64)
        qc = np.array([x[3] for x in latency], dtype=np.float64)
        print(f"samples {len(d)} (one per {MARKER_PERIOD/RATE:.0f}s marker)")
        for nm, v in (("min", d.min()), ("med", np.median(d)),
                      ("p95", np.percentile(d, 95)), ("max", d.max())):
            print(f"  {nm:3s}: {v*1000:8.2f} ms  = {v*RATE:8.0f} frames")
        print(f"our playback queue at write: med {np.median(q):.0f} frames "
              f"({np.median(q)/RATE*1000:.1f} ms) max {q.max():.0f}")
        print(f"capture ring at read       : med {np.median(qc):.0f} frames "
              f"({np.median(qc)/RATE*1000:.1f} ms) max {qc.max():.0f}")
        print("total = our queue + USB framing + device pipeline + capture "
              "ring;\nqueues are shown so the figure is not our own ring "
              "mistaken for device\nlatency (that is what an unpaced writer "
              "measured: a 2000 ms backlog).")
    else:
        print("no marker pairs paired -- latency UNMEASURED (void, not zero)")

    verdict = (cmp.defects == 0 and cmp.counts["lost"] == 0
               and cmp.total >= RATE * min(a.minutes, 1.0))
    print("\nBIT-PERFECT: " + ("PASS" if verdict else "FAIL"))
    sys.exit(0 if verdict else 1)


if __name__ == "__main__":
    main()
