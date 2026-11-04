#!/usr/bin/env python3
"""Bit-perfect soak: host -> USB -> XMOS internal path -> I2S boundary.

The device returns, on its capture stream, the exact words it was about to put
on the I2S bus (firmware tap: DEBUG:LOOPback, CSD_LOOPBACK_TAP). The host
compares them against the golden stream it played, at 384 kHz in the 24-bit
container the device actually offers (S32_LE, bits 31..8 significant).

LATENCY IS MEASURED, NOT ASSUMED (owner directive 2026-11-02)
  The delay from host-sent to host-again-received is unknown and non-zero: USB
  buffering, the audiohub double buffer, the mixer, the IN endpoint queue. Guess
  it and every frame is compared against the wrong reference; re-synchronise on
  every mismatch and the rig quietly compares nothing. Alignment is therefore
  ESTABLISHED with a synthetic signal before a single check runs:

    MARKER   the player holds one constant frame (L=0xFFFFFE, R=0x5A5A5A) until
             capture is actually watching, then releases into the counting
             stream. A counting stream can never repeat L, so a long identical
             run is unambiguously the marker and nothing else.
    ANCHOR   the first frame after a >= SYNC_MIN_RUN marker run IS absolute
             index 0. No search, no inference: player and checker agree because
             the marker IS the boundary.
    LATENCY  the player's frame count at that moment -- the queue depth between
             play and capture, reported in frames and microseconds.

  The player must WAIT for the reader. The first version emitted the marker once
  at t=0 and started capturing 0.5 s later -- 192 000 frames on -- so the marker
  had drained and the rig timed out blaming the device.

NO FREE RE-ANCHORING, BUT NO CASCADE EITHER
  The device tells us which frame it played via the embedded counter, so the rig
  reconstructs an ABSOLUTE stream index per captured frame and checks the
  payload against that. Two failure modes had to be designed out:

    re-anchor freely : a rig that resets its expectation whenever the stream
                       disagrees has validated nothing since the last reset --
                       it has only observed that the device is self-consistent,
                       which every device including a broken one is. It also
                       cannot see a device replaying its ring buffer.
    never re-anchor  : one dropped frame then mismatches on every later frame,
                       so a real single drop reports 1.9 million "gaps" and the
                       number stops meaning anything.

  Resolution: the device's absolute index n_dev is recovered from the received
  counter at the epoch NEAREST the running schedule, the payload is checked
  against n_dev, and the schedule advances from n_dev. A drop costs exactly one
  gap and its size is recorded. A backward jump is a real ordering fault and is
  reported as BACK, never absorbed. Because n_dev is absolute, a device that
  drops exactly 2**24 frames -- invisible to a counter kept modulo 2**24, and to
  any stimulus that loops -- shows up as a payload mismatch AND a 2**24 jump.
  Epoch resolution is unambiguous while jumps stay under 2**23 (~11 s); larger
  jumps are counted in `epoch_blind` and reported, not trusted.

FAILURE CLASSES (the split IS the diagnosis)
  GAP   n_dev > schedule   : frames lost between play and capture. Size and
                             maximum recorded.
  BACK  n_dev < schedule   : reordering, or a replayed buffer. Never absorbed.
  SUBST R == 0x000000      : the audiohub underflowWord -- the device put
                             genuine zeros on the I2S bus. The gap the ear
                             cannot hear.
  FLIP  R != expected      : reported as an XOR bit-position histogram. One
                             constant bit position means a systematic fault
                             (shift, mask); scattered bits mean corruption.

THE RIG MUST BE SEEN TO FAIL
  rig_self_test() drives the comparator with injected faults -- clean, three
  drops, a whole-epoch drop, zero substitution, a constant bit flip, a replayed
  buffer -- and a soak refuses to run unless they all classify correctly. A rig
  that has never failed against a known-bad stream has not been tested.

CONSTANT MEMORY ON PURPOSE
  30 min at 384 kHz x 2 ch x 4 B = 5.5 GB. Nothing is buffered: chunks are
  compared vectorised, and only counters plus MAX_REPORT records survive.

COVERAGE LIMIT -- a PASS here is not a PASS on the pins
  The tap sits before the port's bit-reversal/shift-out and the injection after
  the ADC port's shift-in. Port buffer, pin, 39 ohm resistor and trace are
  OUTSIDE this loop: a 1-bit slip in the port config or a clock-phase fault
  passes clean here. Those need the word observed on the wire.
"""
import argparse
import ctypes
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import bit_perfect_common as bpc   # noqa: E402

RATE = bpc.RATE
CH = bpc.CH
BPB = bpc.BYTES_PER_FRAME
WRAP = 1 << 24
CARD = os.environ.get("CSD_CARD", "3")
APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"

SYNC_L = 0xFFFFFE          # reserved: a counting stream never repeats L
SYNC_R = 0x5A5A5A
SYNC_MIN_RUN = 1000        # consecutive marker frames to accept the boundary
CHUNK_FRAMES = 16384
MAX_REPORT = 12
ABORT_AFTER = 400
STALL_LIMIT = 500
EPOCH_SAFE = WRAP // 2     # beyond this, epoch resolution is a guess: say so

alsa = ctypes.CDLL("libasound.so.2")
# Buffers MUST be declared c_void_p: with no argtypes, ctypes refuses a
# memoryview ("Don't know how to convert parameter 2") and the player thread
# dies silently -- its exception printed but the run then sat in the sync wait
# and reported "no marker", which reads like a dead tap rather than a dead
# writer. Both streams' stats printed sent=0, which is what told us the writer
# was the corpse, not the reader.
for _f, _r, _a in (("snd_pcm_format_value", ctypes.c_int, [ctypes.c_char_p]),
                   ("snd_pcm_format_name", ctypes.c_char_p, [ctypes.c_int]),
                   ("snd_pcm_writei", ctypes.c_long,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
                   ("snd_pcm_readi", ctypes.c_long,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
                   ("snd_strerror", ctypes.c_char_p, [ctypes.c_int])):
    getattr(alsa, _f).restype = _r
    getattr(alsa, _f).argtypes = _a
# Container: S32_LE, 24-bit value in bits 31..8 (the descriptor's
# bBitResolution is 24; the low 8 are padding). Verified against the device's
# own hw_params mask, resolved through ALSA's name table -- see _resolve_format.
#
# How this nearly went wrong, and why the resolution is done this way: an early
# enumeration printed formats using a hand-typed enum->name dict, which mislabelled
# enum 10 as "S24_4LE" (ALSA calls it S32_LE) and enum 12 as "S32_LE" (it is
# U32_LE). Acting on that would have retargeted the whole rig to a format that
# does not exist in this ALSA -- snd_pcm_format_value(b"S24_4LE") returns -1 --
# and the rig would have validated a container nobody negotiated. Never label
# an enum from memory: ask the library.
def _resolve_format(want):
    """Resolve a format by name via ALSA's own table, checking both directions.

    Raises rather than letting the rig negotiate an unintended container: a
    bit-perfect rig that silently validates the wrong wire format still prints
    PASS, which is the worst failure this tool can have."""
    for v in range(0, 64):
        nm = alsa.snd_pcm_format_name(v)
        if nm is None:
            continue
        nm = nm.decode()
        if nm.upper() == want.upper():
            if alsa.snd_pcm_format_value(nm.encode()) != v:
                raise SystemExit(f"format resolver disagrees: name {nm!r} "
                                 f"-> {alsa.snd_pcm_format_value(nm.encode())},"
                                 f" table says {v}")
            return v
    raise SystemExit(f"ALSA has no format named {want!r}; refusing to guess")


WIRE_FORMAT = _resolve_format("S32_LE")
SND_ACCESS_RW_INTERLEAVED = 3   # probed: only MMAP(0) and RW_INTERLEAVED(3)


# ===================== the comparator, testable in isolation ===============
class Comparator:
    """Absolute-schedule checker, separated from the ALSA plumbing so the fault
    injection self-test can drive it directly."""

    def __init__(self):
        self.expected_abs = 0
        self.counts = dict.fromkeys(("gap", "back", "subst", "flip"), 0)
        self.reports = []
        self.xor_hist = {}
        self.total = 0
        self.defects = 0
        self.max_jump = 0
        self.min_jump = 0
        self.epoch_blind = 0

    def feed(self, Lw, Rw, pos0):
        """Lw/Rw: int64 arrays of the 24-bit fields. pos0: capture position of
        the first frame (reporting only). Returns True if aborted.

        Reconstruction is DELTA-based, not slot-based. Comparing every frame
        against the schedule slot (n_dev - (e+i)) turns ONE real drop into one
        flagged frame for every frame after it -- a cascade that makes the gap
        count meaningless; comparing everything against the chunk's first slot
        flags a CLEAN stream n-1 times (both bugs were caught here by
        rig_self_test before any hardware time was spent). Instead:
          n_dev[0] is fixed by nearest-epoch recovery against the schedule;
          later frames follow the device's own counter DELTAS.
        A negative delta is the counter wrap (add 2**24) or a genuine replay;
        the payload check is what disambiguates them honestly, and a device
        replaying 2**24 frames ago is indistinguishable from one that just
        dropped 2**24 frames -- both are reported, neither is hidden."""
        n = len(Lw)
        if n == 0:
            return False
        e = self.expected_abs

        # --- absolute-index reconstruction --------------------------------
        dL = np.diff(Lw, prepend=(e - 1) % WRAP)         # counter deltas
        delta = dL.copy()
        delta[delta < 0] += WRAP                          # wrap or replay
        n0 = Lw[0] + np.rint((e - Lw[0]) / WRAP) * WRAP   # nearest epoch
        n_dev = n0 + np.concatenate(([0], np.cumsum(delta[1:])))
        n_dev = n_dev.astype(np.int64)

        # --- schedule alignment at the chunk head ---------------------------
        head_jump = int(n_dev[0]) - e
        # --- frame-to-frame jumps -------------------------------------------
        steps = np.diff(n_dev, prepend=int(n_dev[0]) - 1)  # want all == 1
        bad_steps = np.nonzero(steps != 1)[0]
        if head_jump != 0:
            bad_steps = np.concatenate(([0], bad_steps))
            steps = np.concatenate(([head_jump], steps))

        # --- payload checked against the ABSOLUTE index ---------------------
        _, exp_R = bpc.expected_vec(n_dev.astype(np.uint64))
        exp_R = exp_R.astype(np.int64)
        bad_r = np.nonzero(Rw != exp_R)[0]

        if len(bad_steps):
            sj = steps[bad_steps] - 1        # lost frames per defect (may be <0)
            self.counts["gap"] += int((sj > 0).sum())
            self.counts["back"] += int((sj < 0).sum())
            self.defects += len(bad_steps)
            self.max_jump = max(self.max_jump, int(sj.max()))
            self.min_jump = min(self.min_jump, int(sj.min()))
            self.epoch_blind += int(((sj > EPOCH_SAFE) |
                                     (sj < -EPOCH_SAFE)).sum())
            for k in bad_steps[:max(0, MAX_REPORT - len(self.reports))]:
                self.reports.append(
                    ("gap" if steps[k] > 1 else "back", pos0 + int(k),
                     int(Lw[k]), int(Rw[k]),
                     int((e + k) % WRAP), int(exp_R[k])))
        if len(bad_r):
            zr = int((Rw[bad_r] == 0).sum())
            self.counts["subst"] += zr
            self.counts["flip"] += len(bad_r) - zr
            self.defects += len(bad_r)
            for k in bad_r[:max(0, MAX_REPORT - len(self.reports))]:
                self.reports.append(("subst" if Rw[k] == 0 else "flip",
                                     pos0 + int(k), int(Lw[k]), int(Rw[k]),
                                     int(n_dev[k] % WRAP), int(exp_R[k])))
            for k in bad_r[:4096]:
                x = int(Rw[k]) ^ int(exp_R[k])
                b = 0
                while x:
                    if x & 1:
                        self.xor_hist[b] = self.xor_hist.get(b, 0) + 1
                    x >>= 1
                    b += 1

        # Advance from the device's own index. Not expected+n (that cascades one
        # drop into millions) and not a free per-frame re-anchor (vacuity).
        self.expected_abs = int(n_dev[-1]) + 1
        self.total += n
        return self.defects >= ABORT_AFTER


# ===================== device control ======================================
def scpi(cmd, timeout=10):
    """Positional command + --timeout, the form rig/case_a_mute_ab.py proves.
    csd_diag.py defines no --card/--cmd; invented flags return nothing and arm
    nothing, silently."""
    try:
        p = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                            "--timeout", str(timeout), cmd],
                           capture_output=True, text=True, cwd=APP,
                           timeout=timeout + 10)
    except subprocess.TimeoutExpired:
        return ""
    return (p.stdout or "").strip()


def reg_read(addr):
    """REGister:READ? takes the register base64-encoded and quoted."""
    import base64
    return scpi(f'REGister:READ? "{base64.b64encode(bytes([addr])).decode()}"')


def require(cmd, want=None, what=""):
    """Every arm step verified by read-back. An unchecked armed-state
    assumption is how a mixer build once validated a muted DAC."""
    got = scpi(cmd)
    if want is not None and want not in got:
        die(f"{what}: asked {cmd!r} wanted {want!r} got {got!r}")
    return got


def die(msg, rc=1):
    print(f"FAIL: {msg}")
    sys.exit(rc)


# ===================== ALSA ================================================
def open_pcm(direction, label):
    """Open hw:CARD,0 in the given direction (0=play, 1=capture) forced to
    S32_LE @384k, and VERIFY the negotiation including the format.

    The stream number is 0 for both: this device exposes pcm0p and pcm0c. An
    earlier version passed `direction` as the stream number too, so capture
    asked for hw:3,1 and failed -- a rig bug that reads exactly like a device
    that cannot capture.

    Silent fallback to S16 is the failure that makes a bit-perfect rig validate
    the wrong container while reporting PASS, hence the explicit format check."""
    pcm = ctypes.c_void_p()
    if alsa.snd_pcm_open(ctypes.byref(pcm), f"hw:{CARD},0".encode(),
                         direction, 0) < 0:
        die(f"cannot open {'capture' if direction else 'playback'} "
            f"hw:{CARD},0")
    hp = ctypes.c_void_p()
    alsa.snd_pcm_hw_params_malloc(ctypes.byref(hp))
    alsa.snd_pcm_hw_params_any(pcm, hp)
    for fn, arg in ((alsa.snd_pcm_hw_params_set_access,
                     SND_ACCESS_RW_INTERLEAVED),
                    (alsa.snd_pcm_hw_params_set_format, WIRE_FORMAT),
                    (alsa.snd_pcm_hw_params_set_channels, CH)):
        if fn(pcm, hp, arg) < 0:
            die(f"{label} rejected {fn.__name__} -- this stream does not "
                f"offer it; enumerate the descriptor, do not assume")
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
            f"{alsa.snd_pcm_format_name(WIRE_FORMAT)} -- refusing to validate "
            f"the wrong wire format")
    per = ctypes.c_uint(0)
    alsa.snd_pcm_hw_params_get_period_size(hp, ctypes.byref(per), 0)
    alsa.snd_pcm_prepare(pcm)
    return pcm, per.value


def read_chunk(pcm, buf):
    n = alsa.snd_pcm_readi(pcm, buf, CHUNK_FRAMES)
    if n <= 0:
        alsa.snd_pcm_recover(pcm, n if n < 0 else -1, 1)
        return None
    return bytes(buf[:n * BPB])


def write_all(pcm, data, stats, paced=True):
    """data must be bytes. Slicing a memoryview does NOT convert to the
    c_void_p we declared -- ctypes raises "wrong type" and the player thread
    dies, which surfaces as 'no sync marker' and reads like a dead tap.

    PACED by default. Playback here is USB Audio ASYNC with a feedback
    endpoint, so the DEVICE meters how fast we may send. Letting the writer
    run ahead (measured: 571 k frames/s into a 2000 ms ring) builds a backlog
    that does two kinds of damage: a phase boundary measures audio produced
    seconds earlier, and a latency measurement reports our own queue instead
    of the pipeline. Both were observed, both blamed on the device."""
    off = 0
    while off < len(data):
        if paced:
            av = alsa.snd_pcm_avail_update(pcm)
            if av < 0:
                stats["recovers"] += 1
                alsa.snd_pcm_recover(pcm, av, 1)
                alsa.snd_pcm_start(pcm)
                continue
            if av == 0:
                stats["paced"] = stats.get("paced", 0) + 1
                time.sleep(0.001)
                continue
            room = min(len(data) - off, av * BPB)
        else:
            room = len(data) - off
        w = alsa.snd_pcm_writei(pcm, data[off:off + room], room // BPB)
        if w < 0:
            stats["recovers"] += 1
            if stats["recovers"] > 100:
                return False
            alsa.snd_pcm_recover(pcm, w, 1)
            continue
        off += w * BPB
        stats["sent"] += w
    return True


# ===================== player ==============================================
def player(pcm, stop, release, secs, stats):
    """Hold the marker until capture is watching, then emit a MONOTONIC index
    through bpc.expected_vec -- the same function the checker uses, so the
    generator and the reference cannot drift."""
    import struct
    alsa.snd_pcm_start(pcm)
    marker = struct.pack("<II", SYNC_L << 8, SYNC_R << 8) * CHUNK_FRAMES

    for _ in range(1500):
        if stop.is_set():
            return
        if not write_all(pcm, marker, stats):
            return
        stats["sync_sent"] += CHUNK_FRAMES
        if release.is_set():
            break
    stats["sync_done"] = stats["sync_sent"]

    n = 0
    t0 = time.time()
    while not stop.is_set() and (time.time() - t0) < secs:
        idx = np.arange(n, n + CHUNK_FRAMES, dtype=np.uint64)
        eL, eR = bpc.expected_vec(idx)
        inter = np.empty((CHUNK_FRAMES, 2), dtype="<u4")
        inter[:, 0] = eL.astype(np.uint32) << np.uint32(8)
        inter[:, 1] = eR.astype(np.uint32) << np.uint32(8)
        if not write_all(pcm, inter.tobytes(), stats):
            return
        n += CHUNK_FRAMES
    stats["play_seconds"] = time.time() - t0


# ===================== sync detection ======================================
def find_marker_end(L, R, carry):
    """(index_after_marker, run_length) or (None, run_length).

    The entry test matters: if the PREVIOUS chunk ended inside the marker with
    the run already long enough, this chunk's first non-marker frame IS the
    anchor. Without it, a marker ending exactly on a chunk boundary never
    anchors and the rig times out blaming the device."""
    run = carry
    n = len(L)
    if n == 0:
        return None, run
    if run >= SYNC_MIN_RUN and not (L[0] == SYNC_L and R[0] == SYNC_R):
        return 0, run
    i = 0
    while i < n:
        if L[i] == SYNC_L and R[i] == SYNC_R:
            j = i
            while j < n and L[j] == SYNC_L and R[j] == SYNC_R:
                j += 1
            run += j - i
            i = j
            if run >= SYNC_MIN_RUN:
                return j, run
        else:
            run = 0
            i += 1
    return None, run


# ===================== rig self-test (fault injection) =====================
def rig_self_test():
    """The rig must FAIL on streams it knows are bad and PASS on the one it
    knows is good. Each failure class gets a case, plus the two properties the
    design depends on: no cascade, and an epoch-wrap drop is not invisible."""
    fails = []

    def clean(n0, count):
        idx = np.arange(n0, n0 + count, dtype=np.uint64)
        L, R = bpc.expected_vec(idx)
        return L.astype(np.int64), R.astype(np.int64)

    # 1. clean stream must be silent
    c = Comparator()
    L, R = clean(0, 5000)
    c.feed(L, R, 0)
    if c.defects:
        fails.append(f"clean stream reported {c.defects} defects: {c.counts}")

    # 2. three single-frame drops -> exactly 3 gaps, NO cascade
    L0, R0 = clean(0, 5000)
    keep = np.setdiff1d(np.arange(5000), [100, 2000, 4500])
    c = Comparator()
    c.feed(L0[keep], R0[keep], 0)
    if not (c.counts["gap"] == 3 and c.counts["back"] == 0
            and c.counts["flip"] == 0 and c.counts["subst"] == 0):
        fails.append(f"3 drops gave {c.counts} (want gap=3 only)")

    # 3. whole-epoch drop: modulo-2**24 indexing would call this clean
    La, Ra = clean(0, 300)
    Lb, Rb = clean(2 * WRAP, 300)
    c = Comparator()
    c.feed(np.concatenate([La, Lb]), np.concatenate([Ra, Rb]), 0)
    if c.defects == 0:
        fails.append("a 2**24-frame drop was invisible -- absolute indexing "
                     "is not in use")

    # 4. zero-substituted R (the underflowWord signature)
    L, R = clean(0, 4096)
    R = R.copy()
    R[::512] = 0
    c = Comparator()
    c.feed(L, R, 0)
    if c.counts["subst"] == 0 or c.counts["gap"]:
        fails.append(f"zero substitution gave {c.counts}")

    # 5. one constant bit flipped -> flip class, histogram concentrated on b7
    L, R = clean(0, 4096)
    c = Comparator()
    c.feed(L, R ^ (1 << 7), 0)
    if c.counts["flip"] == 0 or c.xor_hist.get(7, 0) == 0:
        fails.append(f"constant bit flip not localised: {c.counts} "
                     f"{c.xor_hist}")
    if len(c.xor_hist) != 1:
        fails.append(f"single-bit fault spread over {len(c.xor_hist)} bits -- "
                     f"the histogram cannot discriminate systematic faults")

    # 6a. replay inside one call must be LOUD. Which class it lands in depends
    # on counter direction: within a chunk the counter delta reads as a forward
    # jump of ~2**24, so it reports as gap+flip. The load-bearing property is
    # that it is not silent -- asserting a specific class here would be
    # asserting an artefact of how the replay was fed.
    L, R = clean(0, 2000)
    c = Comparator()
    c.feed(np.concatenate([L, L]), np.concatenate([R, R]), 0)
    if c.defects == 0:
        fails.append("a replayed 2000-frame buffer passed clean")

    # 6b. replay at a chunk boundary is the faithful ring-buffer model: the
    # device's DMA ring restarts, so the next chunk HEAD carries a lower
    # absolute index. That is the BACK class and must not be absorbed.
    c = Comparator()
    c.feed(L, R, 0)
    c.feed(L, R, 2000)
    if c.counts["back"] == 0:
        fails.append(f"a chunk-boundary ring replay gave {c.counts} "
                     f"(want back>0)")

    # 7. marker detector: straddling and boundary-exact
    mk_L = np.full(3000, SYNC_L, dtype=np.int64)
    mk_R = np.full(3000, SYNC_R, dtype=np.int64)
    cl_L, cl_R = clean(0, 50)
    e, run = find_marker_end(np.concatenate([mk_L, cl_L]),
                             np.concatenate([mk_R, cl_R]), 0)
    if e != 3000:
        fails.append(f"marker end found at {e}, want 3000")
    # marker ends exactly at chunk boundary -> anchor on next chunk's frame 0
    e2, _ = find_marker_end(cl_L, cl_R, 3000)
    if e2 != 0:
        fails.append(f"marker ending on a chunk boundary anchored at {e2}, "
                     f"want 0")
    # too-short run must NOT anchor
    e3, _ = find_marker_end(mk_L[:200], mk_R[:200], 0)
    if e3 is not None:
        fails.append("a 200-frame marker run anchored; SYNC_MIN_RUN ignored")
    return fails


# ===================== main ================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--skip-arm", action="store_true")
    ap.add_argument("--marker-timeout", type=float, default=25.0)
    ap.add_argument("--rig-selftest", action="store_true",
                    help="inject faults into the comparator and exit")
    args = ap.parse_args()

    if not bpc.self_test():
        die("golden self-test failed -- the reference cannot be distinguished "
            "from a tampered stream, so no verdict means anything")
    fails = rig_self_test()
    if args.rig_selftest:
        for f in fails:
            print(f"  RIG-BUG: {f}")
        print("RIG-SELFTEST:" + ("PASS" if not fails else f"FAIL ({len(fails)})"))
        sys.exit(0 if not fails else 1)
    if fails:
        for f in fails:
            print(f"  RIG-BUG: {f}")
        die("comparator self-test failed -- refusing to run a soak whose rig "
            "has not been seen to fail on a known-bad stream")

    if not args.skip_arm:
        print("=== arming (every step verified by read-back) ===")
        require("GENerator:OFF", what="generator off")
        require("GENerator:STATe?", "0", "generator state")
        require("DIAG:DSP:PRESet 0", what="DSP preset")
        require("DIAG:DSP:PRESet?", "0",
                "DSP must be preset 0 = byte-exact bypass; the compile-time "
                "default is 2, which filters, and that is not the path asked "
                "for")
        require("VOLume:TRIM:LEFT 0", what="trim L")
        require("VOLume:TRIM:RIGHT 0", what="trim R")
        require("VOLume:TRIM?", "0,0",
                "a non-zero trim means we validate an offset DAC")
        # Codec armed-state readbacks. REGister:READ? answers base64
        # (csd_diag_scpi.c), so 0 == "AA==". An unchecked armed state is how a
        # mixer build once validated a muted DAC -- assert the DECODED value.
        # (reg, mask, why): reg 3's volume field is bits [4:0] only (+2 dB at
        # 0 .. -22 dB at 24); the upper bits are unrelated status, so masking
        # the whole register would fail a correctly-configured codec -- caught
        # on the first live run: 0x03 legitimately read 0x50.)
        for reg, mask, why in ((0x07, 0x00000001, "system mute off (bit0)"),
                               (0x0F, 0x00FFFFFF, "digital vol L = 0 dB"),
                               (0x10, 0x00FFFFFF, "digital vol R = 0 dB"),
                               (0x03, 0x0000001F, "analog vol field = max")):
            got = reg_read(reg)
            import base64 as _b64
            try:
                raw = _b64.b64decode(got + "=" * (-len(got) % 4))
                val = int.from_bytes(raw, "big") if raw else None
            except Exception:
                val = None
            if val is None or (val & mask) != 0:
                die(f"reg {reg:#04x} = {got!r} (decoded {val}); masked field "
                    f"must be 0 -- {why}. Refusing to measure an attenuated "
                    f"or muted path.")
            print(f"  reg {reg:#04x} = {got} -> {val:#010x} & {mask:#010x} "
                  f"= 0   ({why})")
        require("DEBUG:LOOPback 1", what="tap arm")
        require("DEBUG:LOOPback:CLEAR", what="tap counter reset")

    parts = scpi("DEBUG:LOOPback?").split(",")
    if len(parts) != 2 or parts[0].strip() != "1":
        die(f"tap not armed (DEBUG:LOOPback? gave {parts!r}) -- the image must "
            f"carry -DCSD_LOOPBACK_TAP")
    fr0 = int(parts[1])
    print(f"tap armed; tx_frames={fr0} (counts per channel: 2 per LRCLK frame)")

    for a in (["sset", "Extension Unit", "true"],
              ["sset", "Mix 2 Playback Volume", "192,192"],
              ["sset", "Mix 2 Playback Switch", "on,on"]):
        subprocess.run(["amixer", "-c", CARD, *a], capture_output=True)

    cap, cper = open_pcm(1, "capture")
    pla, pper = open_pcm(0, "playback")
    print(f"periods: play={pper} capture={cper}")

    stop = threading.Event()
    release = threading.Event()
    stats = {"sent": 0, "recovers": 0, "sync_sent": 0, "sync_done": 0,
             "play_seconds": 0.0}
    pt = threading.Thread(target=player,
                          args=(pla, stop, release, args.minutes * 60 + 60,
                                stats), daemon=True)
    pt.start()

    buf = (ctypes.c_char * (CHUNK_FRAMES * BPB))()

    print("\n=== alignment: holding the marker until capture is watching ===")
    t0 = time.time()
    anchor_L = None
    anchor_sent = 0
    carry = 0
    cap_pos = 0
    stalls = 0
    pending = None
    while anchor_L is None:
        if time.time() - t0 > args.marker_timeout:
            stop.set()
            die(f"no sync marker in {args.marker_timeout}s. Either the tap is "
                f"not driving the capture stream (we would then be comparing "
                f"the real ADC input against our golden file) or playback never "
                f"started. stats={stats}")
        chunk = read_chunk(cap, buf)
        if chunk is None:
            stalls += 1
            if stalls > STALL_LIMIT:
                stop.set()
                die(f"capture produced no data in {stalls} reads")
            continue
        if not release.is_set():
            release.set()
        a = np.frombuffer(chunk, dtype="<u4").reshape(-1, 2)
        L = (a[:, 0] >> 8).astype(np.int64)
        R = (a[:, 1] >> 8).astype(np.int64)
        cap_pos += len(L)
        end, carry = find_marker_end(L, R, carry)
        if end is not None and end < len(L):
            anchor_L = int(L[end])
            anchor_sent = stats["sent"] - stats["sync_done"]
            lat = anchor_sent - 1
            print(f"  marker run >= {carry} frames")
            print(f"  LATENCY measured: {lat:,} frames = "
                  f"{lat / RATE * 1e6:,.1f} us ({lat / RATE * 1e3:,.3f} ms)")
            print(f"  first captured frame L={anchor_L} (expected 0)")
            # The rest of THIS chunk is already counting-stream data and must be
            # checked: dropping it would report every frame as a gap and blame
            # the device for a rig bug.
            pending = chunk[end * BPB:]

    if anchor_L != 0:
        stop.set()
        die(f"the frame after the marker carries L={anchor_L}, expected 0. The "
            f"marker boundary is not where the counting stream starts, so the "
            f"alignment cannot be trusted -- refusing to guess an offset.")

    comp = Comparator()
    stalls = 0

    def handle(chunk, pos0):
        a = np.frombuffer(chunk, dtype="<u4").reshape(-1, 2)
        return comp.feed((a[:, 0] >> 8).astype(np.int64),
                         (a[:, 1] >> 8).astype(np.int64), pos0)

    deadline = time.time() + args.minutes * 60
    aborted = False
    if pending:
        aborted = handle(pending, cap_pos)
    while (not aborted and comp.total < RATE * args.minutes
           and time.time() < deadline):
        chunk = read_chunk(cap, buf)
        if chunk is None:
            stalls += 1
            if stalls > STALL_LIMIT:
                stop.set()
                die(f"capture stalled {stalls} reads after {comp.total:,} "
                    f"frames")
            continue
        stalls = 0
        aborted = handle(chunk, cap_pos)
        cap_pos += len(chunk) // BPB

    stop.set()
    pt.join(timeout=30)
    for p in (cap, pla):
        alsa.snd_pcm_close(p)

    parts = scpi("DEBUG:LOOPback?").split(",")
    fr1 = int(parts[1]) if len(parts) == 2 else -1
    lat = anchor_sent - 1

    print("\n=== result ===")
    print(f"frames checked        : {comp.total:,}")
    print(f"measured latency      : {lat:,} frames ({lat / RATE * 1e6:,.1f} us)"
          f" = {lat / RATE * 1e3:,.3f} ms")
    print(f"frames played (host)  : {stats['sent']:,} "
          f"(marker {stats['sync_done']:,}) in {stats['play_seconds']:.1f} s, "
          f"recovers={stats['recovers']}")
    print(f"tap tx_frames         : {fr0} -> {fr1} (advanced {fr1 - fr0:,}; "
          f"expect ~2x frames checked)")
    print("failure classes       : " + ", ".join(f"{k}={v}"
                                                for k, v in comp.counts.items()))
    print(f"jump size             : max {comp.max_jump:,}, min {comp.min_jump:,}"
          + (f"  [{comp.epoch_blind} beyond epoch-safe resolution]"
             if comp.epoch_blind else ""))
    if comp.reports:
        print("--- first failures (kind, pos, got L,R | expected L,R) ---")
        for k, pos, l, r, el, er in comp.reports:
            print(f"  {k:6s} pos={pos:<11d} got L=0x{l:06X} R=0x{r:06X}  "
                  f"exp L=0x{el:06X} R=0x{er:06X}")
    if comp.xor_hist:
        top = sorted(comp.xor_hist.items(), key=lambda kv: -kv[1])[:8]
        print("R XOR bit histogram  : " +
              ", ".join(f"b{b}:{c}" for b, c in top))

    # Witness: matching bytes while the tap never ran means we compared our own
    # golden file against the real ADC input.
    if fr1 - fr0 < comp.total:
        die(f"tap advanced {fr1 - fr0:,} frames while {comp.total:,} capture "
            f"frames were checked -- the tap is not the source of what we "
            f"compared, so this run proves nothing")
    hard = sum(comp.counts.values())
    if hard:
        print(f"\nBIT-PERFECT: FAIL ({hard} defective frames of {comp.total:,})")
        sys.exit(1)
    if comp.total < RATE * args.minutes * 0.9:
        die(f"only {comp.total:,} frames checked in a {args.minutes} min soak "
            f"-- that is an under-run, not a pass")
    print(f"\nBIT-PERFECT: PASS  {comp.total:,} frames at 384 kHz / 24-bit, "
          f"every word equal at the I2S boundary, latency {lat} frames")
    print("BITPERF-DONE")


if __name__ == "__main__":
    main()
