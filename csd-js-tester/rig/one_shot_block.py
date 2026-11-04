#!/usr/bin/env python3
"""One block out, one block in, compare only after both are finished.

The procedure, stated exactly as it must be done:

  1. PREP A BLOCK of audio in memory. Build it once, as the literal bytes that
     will be handed to ALSA. The expected array IS those bytes -- the test never
     re-derives what it "should have" sent. An earlier rig reconstructed the
     expected samples from a generator function at compare time, so a generator
     that drifted from the writer silently became the device's error rate.
  2. SEND THE WHOLE BLOCK to the DAC while RECORDING the whole return block.
     Two dumb threads: the writer copies bytes out, the reader copies bytes in.
     Neither one analyses anything, because numpy work in either thread shares
     the GIL with the writer feeding a hard real-time ISO endpoint and the
     rig then manufactures the very dropouts it reports.
  3. ONLY ONCE BOTH HAVE COMPLETED, align and compare.
  4. TRACK overflow and underflow of BOTH streams for the whole run, and treat a
     glitch as a VOID, not as a defect. An error rate measured across an ALSA
     xrun is not a measurement of the DAC: it is a measurement of the host
     failing to keep up, which the loopback-card test already proved this rig
     does on its own (196 defects in 30 s with no USB and no firmware in path).

Alignment is measured, not assumed: the capture is split into regions of
constant offset, and every offset change is reported as a measured SLIP. See
align_score.py for why a single global delta is the wrong model.

Usage:
    python rig/one_shot_block.py --minutes 1
    python rig/one_shot_block.py --fixture --minutes 0.5     # loopback card
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import threading
import time

import numpy as np

# The rig selects its card from the environment AT IMPORT TIME (CSD_CARD,
# CSD_PLAY_DEV, CSD_CAP_DEV). Setting them after the import would silently keep
# the default card -- and a fixture run that quietly used the DAC instead of the
# loopback card would report the DAC's defects as the rig's. So the flags are
# parsed and the environment set before the rig module is loaded at all.
_pre = argparse.ArgumentParser(add_help=False)
_pre.add_argument("--fixture", action="store_true")
_pre.add_argument("--card", type=int, default=None)
_pre.add_argument("--play-dev", type=int, default=None)
_pre.add_argument("--cap-dev", type=int, default=None)
_f, _ = _pre.parse_known_args()
if _f.fixture:
    # snd-aloop puts playback on pcm0p and capture on pcm1c, unlike the DAC
    # which has both on subdevice 0.
    os.environ["CSD_CARD"] = str(_f.card if _f.card is not None else 4)
    os.environ["CSD_PLAY_DEV"] = str(_f.play_dev if _f.play_dev is not None else 0)
    os.environ["CSD_CAP_DEV"] = str(_f.cap_dev if _f.cap_dev is not None else 1)
else:
    if _f.card is not None:
        os.environ["CSD_CARD"] = str(_f.card)
    if _f.play_dev is not None:
        os.environ["CSD_PLAY_DEV"] = str(_f.play_dev)
    if _f.cap_dev is not None:
        os.environ["CSD_CAP_DEV"] = str(_f.cap_dev)

import align_score as A
import csd_signal as sig
from bit_perfect_noise import (CHUNK_FRAMES, MARKER_LEN, MARKER_PERIOD,
                               RATE, die, open_pcm, pack_frames, require,
                               scpi, unpack_frames)

# Bytes per frame, DERIVED from the rig's own wire codec rather than hardcoded:
# a literal 8 is a claim about the negotiated format, and the format is what
# open_pcm verifies at runtime. One pack call on a single frame tells us.
FRAME_BYTES = len(pack_frames(np.zeros(1, dtype=np.uint32),
                              np.zeros(1, dtype=np.uint32)))

alsa = ctypes.CDLL("libasound.so.2")

# Ask ALSA what its own state enum means instead of transcribing it. The first
# version of this file hardcoded XRUN=3 / RUNNING=1: both were wrong (the real
# values are 4 and 3), so the tracker counted every RUNNING sample as an xrun,
# reported 164,310 "xruns" on a perfectly healthy stream, and then voided the run
# because the state it called RUNNING never appeared. A guessed enum turns a good
# measurement into a fabricated fault, which is the worst possible failure here.
alsa.snd_pcm_state_name.restype = ctypes.c_char_p


def _state_num(want):
    for s in range(16):
        if alsa.snd_pcm_state_name(s).decode() == want:
            return s
    raise RuntimeError(f"ALSA has no state named {want!r}")


SND_PCM_STATE_XRUN = _state_num("XRUN")
SND_PCM_STATE_RUNNING = _state_num("RUNNING")
SND_PCM_STATE_PREPARED = _state_num("PREPARED")


class AlsaXrunTracker:
    """Per-stream overflow/underflow accounting, read off the ALSA status.

    The documented ALSA idiom (what aplay/arecord themselves use):
      playback underrun  -> state == XRUN, or avail_max <= avail
                            (the buffer ran dry since the last check)
      capture overrun    -> state == XRUN, or overrange > 0
                            (frames were lost because the app did not read)
    Both are counted for the whole run. A nonzero count voids the run: the
    stream that glitched cannot be used to characterise the device.
    """

    # Detection is EVENT-based, not sample-based. Polling a healthy stream 164k
    # times must report zero glitches: the first version incremented a counter on
    # every poll whose state matched, so a single xrun that lingered for 500 polls
    # was reported as 500, and one transient backlog was reported as thousands of
    # underruns -- an error count that cannot be compared with anything. What is
    # gated on is transitions into XRUN plus the kernel's own cumulative lost-frame
    # counter (overrange), which are authoritative. The per-poll metrics are kept
    # as diagnostics only.
    _XRUN_EDGE = 1

    def __init__(self, pcm, is_play, label):
        self.pcm = pcm
        self.is_play = is_play
        self.label = label
        # Set by the pipes, declared here so a summary printed before a thread
        # finished cannot raise AttributeError and mask the real result.
        self.elapsed = None
        self.wrote = None
        self.read = None
        self.timed_out = False
        self.pending_avail = None
        # Which ALSA states were ever observed. Printed because "never RUNNING"
        # is a different failure from "ran and glitched", and conflating them
        # sends you debugging the wrong thing.
        self.states = set()
        self.underruns = 0        # polls showing the buffer drained (diagnostic)
        self.overruns = 0         # frames the kernel dropped (authoritative)
        self.xruns = 0            # transitions INTO xrun (authoritative)
        self.xrun_samples = 0     # polls observed in xrun state (diagnostic)
        self._was_xrun = False
        self.recovers = 0
        self.short_writes = 0
        self.min_avail = None       # least headroom ever seen
        self.max_pending = None     # most data waiting ever seen
        self.status = ctypes.c_void_p()
        # snd_pcm_status_t is an opaque struct; ALSA gives us an alloca helper.
        self._buf = ctypes.create_string_buffer(256)

    def snapshot(self):
        """Ask ALSA what happened since the last call. Returns (avail, pending)."""
        st = self._buf
        if alsa.snd_pcm_status(self.pcm, ctypes.byref(st)) < 0:
            return None, None
        state = alsa.snd_pcm_status_get_state(ctypes.byref(st))
        avail = alsa.snd_pcm_status_get_avail(ctypes.byref(st))
        amax = alsa.snd_pcm_status_get_avail_max(ctypes.byref(st))
        over = alsa.snd_pcm_status_get_overrange(ctypes.byref(st))
        self.states.add(int(state))
        if state == SND_PCM_STATE_XRUN:
            self.xrun_samples += 1
            if not self._was_xrun:
                self.xruns += 1          # one event, however long it lingers
            self._was_xrun = True
        else:
            self._was_xrun = False
        # The avail_max/overrange idiom only MEANS anything while the stream is
        # RUNNING. On a prepared-but-never-started stream avail_max <= avail is
        # true on every single poll, so a stream that was never started reported
        # 164,219 underruns in 60 s -- the tracker itself inventing the fault it
        # exists to detect. Count only what the hardware was actually doing.
        if state != SND_PCM_STATE_RUNNING:
            return avail, amax
        if self.is_play:
            # avail = free space to write into. avail_max <= avail means the
            # buffer drained to empty at some point since the last look: the
            # device asked for frames we had not delivered -- an underrun.
            if amax <= avail:
                self.underruns += 1
            self.min_avail = avail if self.min_avail is None \
                else min(self.min_avail, avail)
        else:
            # avail = frames readable. overrange counts frames the kernel had
            # to drop because nobody read them: an overrun.
            if over:
                self.overruns += int(over)
            pend = self.pending_avail
            self.max_pending = pend if self.max_pending is None \
                else max(self.max_pending, pend)
        return avail, amax

    pending_avail = None   # set by the reader each loop: frames waiting to read

    def recover(self, err):
        self.recovers += 1
        return alsa.snd_pcm_recover(self.pcm, err, 1)

    def summary(self):
        names = [alsa.snd_pcm_state_name(s).decode() for s in sorted(self.states)]
        return (f"{self.label}: xrun_events={self.xruns} "
                f"xrun_samples={self.xrun_samples} overrun_frames={self.overruns} "
                f"drain_polls={self.underruns} recover={self.recovers} "
                f"short_write={self.short_writes} "
                f"min_avail={self.min_avail} max_pending={self.max_pending} "
                f"states={names}")

    @property
    def clean(self):
        # Never having run is not "clean": it is no measurement at all.
        ran = SND_PCM_STATE_RUNNING in self.states
        # drain_polls is deliberately NOT gated on: on a buffer this shallow it
        # fires whenever the ring empties at all, which for a prefill-then-drain
        # start is expected and is not the device losing frames. The frame-losing
        # signals are xrun_events and overrange, both reported by ALSA itself.
        return (ran and not self.timed_out and self.xruns == 0
                and self.overruns == 0 and self.recovers == 0
                and self.short_writes == 0)


# ---------------------------------------------------------------------------
# The block, built once.
# ---------------------------------------------------------------------------
def build_block(n_frames: int, marker_period: int = MARKER_PERIOD,
                marker_len: int = MARKER_LEN):
    """Return (expected_L, expected_R, bytes) for the block we are about to send.

    The bytes ARE the expectation: they are produced once here and both written
    to ALSA and kept for comparison. Nothing downstream recomputes the signal.
    Marker blocks are embedded at fixed schedule positions so alignment has
    landmarks; the rest is deterministic band-limited noise, unique per frame,
    so an off-by-N alignment cannot accidentally match.
    """
    idx = np.arange(n_frames, dtype=np.uint64)
    # sig.expected_vec is the rig's single definition of the stimulus, reused
    # rather than reimplemented: an independent copy is how a rig ends up
    # comparing the device against a stream nobody played.
    L, R = sig.expected_vec(idx)
    L = L.astype(np.int64)
    R = R.astype(np.int64)
    m = (idx % np.uint64(marker_period)) < np.uint64(marker_len)
    L[m] = sig.SYNC_L
    R[m] = sig.SYNC_R

    # pack_frames/unpack_frames are the rig's ONLY wire codec (S32_LE, 24-bit
    # value in bits 31..8, 8 bytes per frame). Hand-rolling a 24-bit-packed
    # codec here was the first draft's bug: it produced 6 bytes/frame, so the
    # block was read back misaligned and every frame after the first would have
    # "failed" while the device was delivering correctly. Going through the same
    # codec the reader uses makes that class of bug structurally impossible.
    raw = pack_frames(L.astype(np.uint32), R.astype(np.uint32))
    rL, rR = unpack_frames(raw)
    if not (np.array_equal(rL, L) and np.array_equal(rR, R)):
        raise AssertionError("block does not round-trip through the wire codec")
    assert len(raw) == n_frames * FRAME_BYTES, (len(raw), n_frames)
    return L, R, raw


def sent_lookup(expL, expR):
    """The expectation as a lookup, so align_score stays reusable.

    Unlike a generator, this answers only for indices actually inside the
    block. Outside the block there is no expectation: a probe landing past the
    end returns nothing, which makes the aligner discard the region instead of
    comparing it against invented content.
    """
    n = len(expL)

    def f(idx):
        idx = np.asarray(idx, dtype=np.int64)
        inside = (idx >= 0) & (idx < n)
        outL = np.zeros(len(idx), dtype=np.int64)
        outR = np.zeros(len(idx), dtype=np.int64)
        ok = np.nonzero(inside)[0]
        if len(ok):
            j = idx[ok]
            outL[ok] = expL[j]
            outR[ok] = expR[j]
        # Mark out-of-block so a caller can tell "no expectation" from "zero".
        outL[~inside] = 0x7FFFFFFFFFFFFFF0
        outR[~inside] = 0x7FFFFFFFFFFFFFF0
        return outL, outR
    return f


# ---------------------------------------------------------------------------
# The two dumb pipes. No analysis in either.
# ---------------------------------------------------------------------------
def arm_device():
    """Every gate the proven rig applies, before a single frame is played.

    Reused rather than re-derived, because each of these was learned the
    expensive way and a bit-exact verdict is only falsifiable with all of them
    in place:

      trim 0            -- a non-zero trim validates an offset DAC
      DSP preset 0      -- the FIR rewrites EVERY sample (csd_dsp.xc:187 only
                           returns early for preset 0), so at any other preset
                           we would compare noise against a filtered copy of
                           itself and the test could never fail
      mixer gain unity  -- otherwise the words we compare were multiplied first
      codec 0 dB/unmute -- otherwise we measure an attenuated path
      generator OFF     -- the internal generator would sum into the stream
      tap armed         -- see arm_tap: without this the capture is silence
    """
    require("VOLume:TRIM:LEFT 0", what="trim L")
    require("VOLume:TRIM:RIGHT 0", what="trim R")
    require("VOLume:TRIM?", "0,0",
            "a non-zero trim means we validate an offset DAC")
    require("DIAG:DSP:PRESet 0", what="DSP preset set")
    got = scpi("DIAG:DSP:PRESet?")
    if got.split(",")[0].strip() != "0":
        die(f"DSP preset is {got!r}, not 0 -- the FIR rewrites every sample, "
            f"so the device would not be bit-exact with itself by construction")
    got = scpi("MIXer:GAIN?")
    # NOTE: an earlier version tested `got.lower().startswith(("err", ""))`,
    # which is TRUE for every string -- "" is a prefix of all strings -- so this
    # gate NEVER RAN and every run, including MIXER=1 builds, printed "NO MIXER
    # IN THIS BUILD". A gate that silently skips is worse than no gate: it reads
    # as verified. Detect a missing row by an actual error/empty reply instead.
    missing = (not got) or got.lower().startswith(("err", "undefined", "unknown"))
    if not missing:
        require("MIXer:GAIN:HOST 32768", what="mixer host gain to unity")
        got = scpi("MIXer:GAIN?")
        if got.split(",")[0].strip() != "32768":
            die(f"mixer host gain is {got!r}, not Q15 unity 32768 -- the stream "
                f"reaching I2S is scaled, so it cannot match what we sent")
        print(f"  mixer host gain = unity ({got})")
    else:
        print(f"  mixer host gain: row absent ({got!r}) -- nothing scales the "
              "stream digitally; codec readback below is the remaining gate")
    import base64
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
            die(f"reg {reg:#04x} = {got!r} ({val}); masked field must be 0 -- "
                f"{why}. Refusing to measure an attenuated or muted path.")
        print(f"  reg {reg:#04x} -> {val:#010x} & {mask:#010x} = 0 ({why})")
    require("GENerator:STATe OFF", what="function generator off")


def arm_tap(label="tap"):
    """Arm the DAC->ADC tap and read back the device-side witness.

    The tap is opted into BY NAME and is never automatic (csd_diag_scpi.c:1480),
    so a tool that forgets to arm it captures pure silence -- which is exactly
    what this tool did: both MIXER=0 runs returned 100% zero fields and a VOID,
    and every `xrun --io` reload resets the arm. The earlier good run only worked
    because a previous bit_perfect_noise.py invocation had armed it, an accident
    of process history that made the result look like a property of the image.

    tx_frames is the reason arming is not enough. The contract requires the
    counter to have ADVANCED across the run: a capture that matches a tap which
    never executed is the rig echoing itself. Returns (enable, tx_frames).
    """
    def ask(cmd):
        r = scpi(cmd)
        return r.strip() if r else r

    print(f"  arming: {label}")
    if ask("DEBUG:LOOPback 1") is None:
        raise RuntimeError("DEBUG:LOOPback 1 got no reply -- cannot arm the tap")
    if ask("DEBUG:LOOPback:CLEAR") is None:
        raise RuntimeError("DEBUG:LOOPback:CLEAR got no reply")
    st = ask("DEBUG:LOOPback?")
    try:
        en, fr = (int(x) for x in st.split(",")[:2])
    except Exception:
        raise RuntimeError(f"unparsable DEBUG:LOOPback? reply {st!r}")
    if en != 1:
        raise RuntimeError(f"tap did not arm: DEBUG:LOOPback? -> {st!r}")
    print(f"  tap armed, tx_frames reset to {fr}")
    return en, fr


def read_tx_frames():
    st = scpi("DEBUG:LOOPback?")
    try:
        return int(st.split(",")[1])
    except Exception:
        return -1


def prefill(pcm, tr: AlsaXrunTracker, block: bytes, want_frames: int):
    """Fill the playback ring synchronously BEFORE capture starts.

    Capture started over an empty cable goes straight to XRUN and never recovers
    on its own: the fixture run observed 167k XRUN samples and never once
    reached RUNNING on either stream. The DAC does not have this problem because
    its capture endpoint is driven by the device clock and the host ring holds
    768000 frames, but a kernel loopback card needs data to exist before there
    is anything to read. Filling half the buffer first is the ordering the
    hardware actually wants.
    """
    off = 0
    while off < want_frames * FRAME_BYTES:
        av = alsa.snd_pcm_avail_update(pcm)
        if av < 0:
            if tr.recover(int(av)) < 0:
                break
            continue
        take = min(CHUNK_FRAMES, (want_frames * FRAME_BYTES - off) // FRAME_BYTES,
                   av)
        if take <= 0:
            time.sleep(0.0005)
            continue
        w = alsa.snd_pcm_writei(pcm, block[off:off + take * FRAME_BYTES], take)
        if w < 0:
            if tr.recover(int(w)) < 0:
                break
            continue
        off += int(w) * FRAME_BYTES
    return off // FRAME_BYTES


def writer(pcm, tr: AlsaXrunTracker, block: bytes, n_frames: int,
           stop: threading.Event, start_off: int = 0):
    """Hand the whole block to ALSA, in order, blocking. No other work."""
    total = len(block)
    off = start_off * FRAME_BYTES
    t0 = time.monotonic()
    while off < total and not stop.is_set():
        n_avail = alsa.snd_pcm_avail_update(pcm)
        if n_avail < 0:
            if tr.recover(int(n_avail)) < 0:
                stop.set()
                return
            continue
        tr.snapshot()
        take = min(CHUNK_FRAMES, n_frames - off // FRAME_BYTES, n_avail)
        if take <= 0:
            time.sleep(0.0005)
            continue
        seg = block[off:off + take * FRAME_BYTES]
        w = alsa.snd_pcm_writei(pcm, seg, take)
        if w < 0:
            if tr.recover(int(w)) < 0:
                stop.set()
                return
            continue
        if w != take:
            # A short write means ALSA could not take the whole chunk: the host
            # is not keeping the pipe full. Recorded, and it voids the run.
            tr.short_writes += 1
        off += int(w) * FRAME_BYTES
    tr.elapsed = time.monotonic() - t0
    tr.wrote = off // FRAME_BYTES


def reader(pcm, tr: AlsaXrunTracker, out: list, want_frames: int,
           stop: threading.Event, wall_limit: float,
           write_done=None, drain_ms=400.0):
    """Copy frames in, verbatim. Never unpacks, never compares.

    Ends when it has what it asked for, or -- once the writer has handed over the
    whole block -- when the ring has been empty for drain_ms. Without that second
    condition the reader spins for the rest of the wall limit after the block
    ends and then sets timed_out, which voids a run that actually completed.
    """
    buf = ctypes.create_string_buffer(CHUNK_FRAMES * FRAME_BYTES)
    got = 0
    t0 = time.monotonic()
    idle_since = None
    while got < want_frames and not stop.is_set():
        if time.monotonic() - t0 > wall_limit:
            tr.timed_out = True
            tr.read = got
            break
        n_avail = alsa.snd_pcm_avail_update(pcm)
        if n_avail < 0:
            if tr.recover(int(n_avail)) < 0:
                stop.set()
                return
            continue
        tr.pending_avail = int(n_avail)
        tr.snapshot()
        if n_avail == 0:
            if write_done is not None and write_done.is_set():
                if idle_since is None:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since > drain_ms / 1000.0:
                    break        # block fully delivered and drained
            time.sleep(0.0005)
            continue
        idle_since = None
        take = min(CHUNK_FRAMES, want_frames - got, int(n_avail))
        n = alsa.snd_pcm_readi(pcm, buf, take)
        if n < 0:
            if tr.recover(int(n)) < 0:
                stop.set()
                return
            continue
        out.append(bytes(buf.raw[:int(n) * FRAME_BYTES]))
        got += int(n)
    tr.elapsed = time.monotonic() - t0
    tr.read = got


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=1.0)
    # --fixture/--card/--play-dev/--cap-dev are consumed by _pre above, before
    # the rig import, so they are declared there and not repeated here.
    # No --seed: the stimulus is the rig's fixed generator on purpose, so the
    # block is byte-identical to what the proven rig plays. A flag that was
    # accepted and ignored would imply a knob that does not exist.
    ap.add_argument("--probe-stride", type=int, default=A.PROBE_STRIDE,
                    help="frames between offset probes; must stay well under "
                         "48*8 to see a single dropped packet boundary")
    ap.add_argument("--skip-arm", action="store_true",
                    help="do not apply the device gates/arm the tap (a run "
                         "without them is unfalsifiable; use only when the "
                         "device is known armed)")
    ap.add_argument("--no-prefill", action="store_true",
                    help="start capture over an empty ring (tests whether "
                         "delta tracks the prefill; may XRUN the capture)")
    ap.add_argument("--allow-stream-glitch", action="store_true",
                    help="report a defect rate even if ALSA lost frames "
                         "(default: refuse -- it would not be a measurement)")
    a, _extra = ap.parse_known_args(argv)
    # _extra holds the card-selection flags already consumed by _pre above.

    n_frames = int(RATE * 60 * a.minutes)
    # Round up to whole chunks so the block is an integer number of writes.
    n_frames = ((n_frames + CHUNK_FRAMES - 1) // CHUNK_FRAMES) * CHUNK_FRAMES
    print(f"=== prepping block: {n_frames:,} frames "
          f"({n_frames/RATE:.1f}s @ {RATE}, {FRAME_BYTES} B/frame) ===")
    t0 = time.monotonic()
    expL, expR, block = build_block(n_frames)
    print(f"  built in {time.monotonic()-t0:.1f}s, "
          f"{len(block)/1e6:.0f} MB, markers every {MARKER_PERIOD:,} frames")

    # The rig verifies the negotiation inside open_pcm and dies if ALSA gave us
    # anything but S32_LE at RATE, so a mismatched format can never be silently
    # validated. It returns (pcm, period_size, buffer_size) -- the sizes are
    # printed because the buffer depth is what bounds how long the device can be
    # starved before it is an underrun rather than a schedule change.
    pd, play_per, play_buf = open_pcm(0, "playback")
    cd, cap_per, cap_buf = open_pcm(1, "capture")
    print(f"  streams open: playback period={play_per} buffer={play_buf}; "
          f"capture period={cap_per} buffer={cap_buf}")
    tx_before = None
    if _f.fixture:
        print("FIXTURE MODE: kernel loopback card -- no device, no USB, no "
              "firmware in the path. A defect here belongs to the tester.")
        print("  device gates and tap arming SKIPPED (no device). There is no "
              "device-side witness in this mode: it validates the rig and the "
              "host ALSA path only, and cannot certify the DAC.")
    elif a.skip_arm:
        print("  WARNING: --skip-arm. Device gates and tap arm NOT applied by "
              "this run. If the tap was not already armed the capture is "
              "silence, and the tx_frames witness below is the only thing "
              "standing between that and a false verdict.")
    else:
        arm_device()
    # Arm as late as possible and read the witness immediately: every `xrun
    # --io` reload resets the tap, so arming is part of THIS run's proof and
    # cannot be inherited from whatever ran before it.
    if not _f.fixture and not a.skip_arm:
        arm_tap()
    if not _f.fixture:
        tx_before = read_tx_frames()
        print(f"  witness: tx_frames before run = {tx_before}")

    trw = AlsaXrunTracker(pd, True, "playback")
    trr = AlsaXrunTracker(cd, False, "capture")
    stop = threading.Event()
    captured: list = []

    # Both streams need an explicit snd_pcm_start. open_pcm only PREPAREs them,
    # and in RW_INTERLEAVED polled mode nothing else starts them: the first run
    # of this tool read 0 frames in 180 s and reported 164k phantom underruns
    # because both streams were still PREPARED. Capture starts first: frames the
    # device emits before the reader is running are gone for good, and a lost
    # head is a lost alignment.
    alsa.snd_strerror.restype = ctypes.c_char_p

    def start(pcm, which, fatal):
        rc = alsa.snd_pcm_start(pcm)
        if rc < 0:
            msg = alsa.snd_strerror(int(rc)).decode(errors="replace")
            print(f"  snd_pcm_start({which}) -> {int(rc)} ({msg})")
            if fatal:
                print(f"RESULT: VOID -- could not start {which}")
                sys.exit(2)
            # Capture MUST be started explicitly: in polled RW_INTERLEAVED mode
            # nothing else starts it, and the first version of this tool read 0
            # frames in 180 s. Playback does NOT need it -- the very first run
            # wrote all 23 M frames with no explicit start, because writei
            # prepares and starts implicitly -- so a refusal here is reported and
            # survived, and the tracker's requirement that RUNNING was observed
            # is what keeps a silent non-start from passing as a clean run.
            return False
        return True

    # Order matters and the fixture run is what proved it: starting capture over
    # an empty cable left both streams stuck in XRUN for the whole minute
    # (167k XRUN samples, RUNNING never observed), while 23M frames still
    # "transferred" -- a run that looks busy and measures nothing. Fill the
    # playback ring first, then start capture, then let the writer resume.
    write_done = threading.Event()
    # --no-prefill probes startup behaviour, not alignment. Measured on both
    # targets: loopback gave delta = +384 with a 131,072-frame prefill and the
    # DAC gave delta = -236 with a 384,000-frame prefill. Delta tracks NEITHER,
    # so it is ~0 in both -- the tap emits schedule frame k as the device plays
    # frame k, and prefill only sets how far the WRITER sits ahead of the device,
    # which is invisible to capture. An earlier note here claimed delta should
    # move with the prefill; the two runs disproved that, so the flag must not be
    # read as a latency control.
    prefill_frames = 0 if a.no_prefill else min(play_buf // 2, n_frames)
    wrote = prefill(pd, trw, block, prefill_frames)
    print(f"  prefilled playback with {wrote:,} frames, then starting capture")
    start(cd, "capture", fatal=True)
    tr = threading.Thread(target=reader,
                          args=(cd, trr, captured, n_frames + 4 * CHUNK_FRAMES,
                                stop, a.minutes * 60 + 120, write_done),
                          daemon=True)
    tr.start()
    time.sleep(0.25)
    start(pd, "playback", fatal=False)
    def _run_writer():
        writer(pd, trw, block, n_frames, stop, wrote)
        write_done.set()          # the reader may now finish on an empty ring

    tw = threading.Thread(target=_run_writer, daemon=True)
    tw.start()
    tw.join()
    tr.join()

    el = time.monotonic() - t0
    # Snapshot BEFORE closing: the device's own audio loop free-runs after the
    # host stream stops and would inflate the counters we just read.
    print("\n=== stream health (tracked for the whole run) ===")
    print("  " + trw.summary())
    print("  " + trr.summary())
    print(f"  wrote {getattr(trw,'wrote',0):,} frames in "
          f"{getattr(trw,'elapsed',0):.1f}s; "
          f"read {getattr(trr,'read',0):,} frames in "
          f"{getattr(trr,'elapsed',0):.1f}s; wall {el:.1f}s")
    alsa.snd_pcm_close(pd)
    alsa.snd_pcm_close(cd)

    # The device-side witness. tx_frames is incremented by the tap itself, so
    # this is the only evidence that the DAC->ADC copy actually executed. A
    # capture that matches while the counter sat still means the rig echoed its
    # own output -- the comparison would be vacuous and its PASS meaningless.
    tx_after = None
    if not _f.fixture:
        tx_after = read_tx_frames()
        adv = (tx_after - tx_before) if (tx_after is not None and tx_before is not None
                                         and tx_after >= 0 and tx_before >= 0) else None
        print(f"  witness: tx_frames after run = {tx_after} (advanced {adv})")
        if adv is None or adv <= 0:
            print("\nRESULT: VOID -- the loopback tap did not advance. The tap "
                  "never ran, so this capture is not evidence about the DAC.")
            sys.exit(2)

    glitch = not (trw.clean and trr.clean)
    if glitch and not a.allow_stream_glitch:
        print("\nRESULT: VOID -- the tester lost frames at the ALSA level "
              "(see stream health). An error rate measured across that is a "
              "measurement of the host, not of the DAC.")
        sys.exit(2)

    print(f"\n=== comparing {len(captured):,} captured chunks, post-stream ===")
    got = sum(len(c) for c in captured) // FRAME_BYTES
    capL = np.empty(got, dtype=np.int64)
    capR = np.empty(got, dtype=np.int64)
    off = 0
    for raw in captured:
        L, R = unpack_frames(raw)
        k = len(L)
        capL[off:off + k] = L
        capR[off:off + k] = R
        off += k
    captured.clear()
    if got < CHUNK_FRAMES:
        print("RESULT: VOID -- nothing captured")
        sys.exit(2)

    lookup = sent_lookup(expL, expR)
    # centre_guess: the writer runs ahead of the reader by roughly one host
    # ring, so capture frame i is expected near schedule frame i + buffer.
    al = A.align(capL, capR, lookup, sig.SYNC_L, sig.SYNC_R, RATE,
                 centre_guess=play_buf, marker_period=MARKER_PERIOD,
                 probe_stride=a.probe_stride)
    print(f"alignment: {al['verdict']}")
    for k in ("markers", "n_captured", "delta", "latency_frames",
              "latency_ms", "head_discarded", "tail_discarded",
              "aligned_frames", "aligned_fraction", "probes",
              "slips_unverified", "slip_band_frames"):
        if k in al:
            print(f"  {k:22s} {al[k]}")
    if al["verdict"] != "ALIGNED":
        print(f"  reason: {al['reason']}")
        # A VOID that only says "no markers" cannot be acted on: the same words
        # cover a dead capture, a stuck tap replaying one packet, and a genuinely
        # shifted stream, and they call for opposite responses. Say what IS there.
        nz = int((capL == 0).sum() + (capR == 0).sum())
        print(f"  capture content: {nz:,} zero fields of {2*got:,} "
              f"({100.0*nz/max(1,2*got):.1f}%)")
        uvals, ucnt = np.unique(capL[:1 << 20], return_counts=True)
        order = np.argsort(-ucnt)[:5]
        print("  most common L values in first 1M frames: "
              + ", ".join(f"0x{uvals[i]:06X}x{ucnt[i]}" for i in order))
        if nz == 2 * got:
            print("  the capture is entirely silent -- the device delivered "
                  "nothing; this is a dead stream, not a defective one")
        print("RESULT: VOID -- unaligned; no error rate is reported")
        sys.exit(2)
    for s in al["slips"][:10]:
        print(f"  SLIP at capture {s['at_capture']:,}: "
              f"{s['frames_lost']:+,} frames ({s['direction']})")
    if al["slips"]:
        print(f"  total slips: {len(al['slips'])} -> the stream is NOT one "
              f"continuous delivery of the block; each is a real dropout")

    sc = A.score(capL, capR, lookup, al["regions"])
    print("\n=== RESULT (aligned regions only) ===")
    print(f"frames checked : {sc['checked']:,} of {got:,} captured "
          f"({100.0*sc['checked']/max(1,got):.1f}%)")
    print(f"defects        : {sc['defects']}  "
          f"rate={sc['defects']/max(1,sc['checked']):.3e}")
    print(f"zero fields    : {sc['zeros']:,}")
    print(f"run histogram  : {sc['run_histogram'][:8]}")
    for (p, gl, gr, el_, er) in sc["reports"][:8]:
        print(f"  pos={p:,} got=0x{gl:06X},0x{gr:06X} "
              f"want=0x{el_:06X},0x{er:06X}")
    ok = (sc["defects"] == 0 and not al["slips"]
          and al["aligned_fraction"] >= A.MIN_SCORED_FRACTION
          and not glitch)
    print("\nBIT-PERFECT: " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
