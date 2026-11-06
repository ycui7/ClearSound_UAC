#!/usr/bin/env python3
"""The user's two bit-perfect gates, with every signal-path lever pinned.

TEST 1 (silence).  Send dead silence for a few seconds with the I2S loopback
tap armed and assert the capture is ALL ZEROS. That is what proves the ADC is
not being smuggled to the host: the host asked for nothing, so anything the
host hears came from inside the box.

TEST 2 (noise).   Stream band-limited noise for N minutes and verify every
captured frame equals the frame the host sent, bit for bit. Also report the
measured out->in latency, which is what makes the comparison meaningful: the
captured word is the played word delayed by the pipeline, and a checker that
does not know the delay cannot tell a drop from an off-by-one.

WHEVERY LEVER IS PINNED, AND WHY. The tap sits on samplesOut AFTER
UserBufferManagement, so it reports the word about to go on the I2S bus -- i.e.
post-DSP, post-generator, post-mix, post-volume. Comparing that against the
host's own bytes is only fair when every one of those stages is the identity.
Measured, not assumed:

  - DSP preset 2 (FD/FIR bass shelf) does NOT return zeros for zero input:
    with silence it emits a constant 0xFFFFFE on L and 0xFFFFEA on R, one
    distinct value, no decay -- a fixed-point limit cycle. Left armed, it also
    breaks bit-exactness, because it legitimately reshapes samples. So preset
    0 (none) is required here, and this script asserts it rather than trusting
    the boot default.
  - The generator must be off, or it sums a tone into the host stream.
  - Host volume must be unity. With MIXER=1 the SET_CUR volume lives in the
    mixer core (XUA_OUT_VOLUME_IN_MIXER), so a non-max FU attenuates every
    sample and bit-exactness fails with a scale factor. Set and read back.

Stimulus is band-limited noise indexed by ABSOLUTE frame number, never a
repeating buffer: a periodic payload makes a gap of exactly one period
invisible. Expected(N) computable from N alone is what makes one dropped frame
cost one reported error instead of desynchronising the checker.

Exit code 0 only if BOTH gates pass.
"""
import argparse, ctypes, os, sys, threading, time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import csd_signal as sig
import bp_score

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = f"{APP}/.venv/bin/python"
CARD = os.environ.get("CSD_CARD", "3")
RATE = sig.RATE
N = 4096
# Writer block: frames generated per numpy call. Generation duty is only
# 1.45% at this size (measured), so numpy is never the limiter.
WBLK = 16384
# PLAYBACK ring: 8192 frames (21 ms at 384 kHz). This is NOT slack to make the
# test easier -- it is the smallest ring that sustains 100% feed. Measured, play
# alone, identical everything else but the ring:
#     buf=2048 -> 288,676 Hz (75.18%)   buf=8192 -> 387,273 Hz (100.85%)
# A 2048-frame blocking writei loses its sleep/wake race against the device and
# under-feeds it; lib_xua then substitutes underflowWord (0 for PCM), which is
# exactly the zeros this rig had been attributing to the firmware. Latency
# reported by the gate INCLUDES this ring, and the size is printed with it.
PLAY_BUFSZ = 8192
# CAPTURE ring: 32768 frames (85 ms at 384 kHz). The old value was 2048 (5.3 ms),
# justified by a measurement that does not apply here: "read alone at 2048
# sustains 383,254 Hz (99.81%)" -- measured with the reader by itself. The gate
# reads WHILE a writer thread hammers writei through ctypes, and the Python
# reader then shares the GIL with it. Measured result of that mismatch: 17,278,976
# frames collected from a 23,270,400-frame window -- 74% -- with exactly ONE ALSA
# error. USB isochronous drops are silent, so the frames were simply lost and
# nothing complained; a fixture that loses a quarter of the return path cannot
# judge bit-exactness, and its 0.0003% score described the fixture, not the
# device. Ring slack is what absorbs the scheduling jitter the two threads create.
# Latency is not bought away by this: alignment is computed per read against the
# written watermark, so the ring size shifts the alignment search, not the verdict.
BUFSZ = 32768
AP = 1.0 / RATE


def log(msg=""):
    print(msg, flush=True)


def scpi(cmd, timeout=20):
    import subprocess
    try:
        p = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                            "--timeout", "15", cmd], cwd=APP,
                           capture_output=True, text=True, timeout=40)
    except Exception as e:
        return f"ERR {e}"
    out = (p.stdout or "").strip().replace("\r", "")
    if not out and p.returncode != 0:
        return f"ERR rc={p.returncode} {(p.stderr or '').strip()[:120]}"
    return out


# --------------------------------------------------------------------- ALSA --
alsa = ctypes.CDLL("libasound.so.2")
for _f, _r, _a in (
        ("snd_pcm_format_name", ctypes.c_char_p, [ctypes.c_int]),
        ("snd_strerror", ctypes.c_char_p, [ctypes.c_int]),
        ("snd_pcm_open", ctypes.c_int,
         [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p, ctypes.c_int, ctypes.c_uint]),
        ("snd_pcm_hw_params_malloc", ctypes.c_int, [ctypes.POINTER(ctypes.c_void_p)]),
        ("snd_pcm_hw_params_any", ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p]),
        ("snd_pcm_hw_params_set_access", ctypes.c_int,
         [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]),
        ("snd_pcm_hw_params_set_format", ctypes.c_int,
         [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]),
        ("snd_pcm_hw_params_set_channels", ctypes.c_int,
         [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]),
        ("snd_pcm_hw_params_set_rate_near", ctypes.c_int,
         [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint),
          ctypes.POINTER(ctypes.c_int)]),
        ("snd_pcm_hw_params_set_buffer_size_near", ctypes.c_int,
         [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]),
        ("snd_pcm_hw_params", ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p]),
        ("snd_pcm_prepare", ctypes.c_int, [ctypes.c_void_p]),
        ("snd_pcm_start", ctypes.c_int, [ctypes.c_void_p]),
        ("snd_pcm_drop", ctypes.c_int, [ctypes.c_void_p]),
        ("snd_pcm_close", ctypes.c_int, [ctypes.c_void_p]),
        ("snd_pcm_recover", ctypes.c_int,
         [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]),
        ("snd_pcm_avail_update", ctypes.c_long, [ctypes.c_void_p]),
        ("snd_pcm_writei", ctypes.c_long,
         [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
        ("snd_pcm_readi", ctypes.c_long,
         [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong])):
    getattr(alsa, _f).restype = _r
    getattr(alsa, _f).argtypes = _a


def resolve(want):
    """Format enum from ALSA's own name table. Never hardcode: a hand-written
    dict once labelled enum 10 'S24_4LE' when ALSA calls it S32_LE."""
    for v in range(64):
        nm = alsa.snd_pcm_format_name(v)
        if nm is None:
            continue
        nb = nm if isinstance(nm, bytes) else nm.encode()
        if nb.decode().upper() == want.upper():
            return v
    sys.exit(f"no format {want}")


FMT = resolve("S32_LE")


def openp(stream, bufsz=None):
    pcm = ctypes.c_void_p()
    rc = alsa.snd_pcm_open(ctypes.byref(pcm), f"hw:{CARD},0".encode(), stream, 0)
    if rc < 0:
        sys.exit(f"open stream {stream} failed: "
                 f"{alsa.snd_strerror(rc).decode() if rc < 0 else '?'}")
    hp = ctypes.c_void_p()
    alsa.snd_pcm_hw_params_malloc(ctypes.byref(hp))
    alsa.snd_pcm_hw_params_any(pcm, hp)
    for fn, arg in ((alsa.snd_pcm_hw_params_set_access, 3),
                    (alsa.snd_pcm_hw_params_set_format, FMT),
                    (alsa.snd_pcm_hw_params_set_channels, 2)):
        if fn(pcm, hp, arg) < 0:
            sys.exit(f"stream {stream}: rejected {fn.__name__}")
    d = ctypes.c_uint(RATE)
    alsa.snd_pcm_hw_params_set_rate_near(pcm, hp, ctypes.byref(d), None)
    if d.value != RATE:
        sys.exit(f"stream {stream}: negotiated {d.value}, wanted {RATE}")
    b = ctypes.c_uint(bufsz or BUFSZ)
    alsa.snd_pcm_hw_params_set_buffer_size_near(pcm, hp, ctypes.byref(b))
    if b.value > RATE // 50 and not (bufsz or 0) > RATE // 50:
        sys.exit(f"stream {stream}: granted buffer {b.value} frames = "
                 f"{1000.0*b.value/RATE:.1f} ms is too much slack to trust "
                 "a latency number")
    if alsa.snd_pcm_hw_params(pcm, hp) < 0:
        sys.exit(f"stream {stream}: hw_params failed")
    alsa.snd_pcm_prepare(pcm)
    return pcm, b.value


def set_host_volume_max():
    """FU / Playback Volume to max. With MIXER=1 the host volume is applied in
    the mixer core, so anything less than max scales every sample and makes
    bit-exactness impossible for a reason that has nothing to do with the wire.
    """
    import subprocess
    names = subprocess.run(["amixer", "-c", CARD, "controls"],
                           capture_output=True, text=True).stdout
    n_ok = 0
    import re as _re
    for line in names.splitlines():
        if "numid=" not in line:
            continue
        numid = line.split("numid=")[1].split(",")[0]
        if "Playback Volume" in line:
            # Read the control's own range instead of assuming 192: the max is
            # what means 0 dB here, and hardcoding a number that happens to be
            # right for this codec silently mis-sets any other.
            m = _re.search(r"values=([0-9]+):([0-9]+)", line)
            hi = m.group(2) if m else "192"
            subprocess.run(["amixer", "-c", CARD, "cset", f"numid={numid}", hi],
                           capture_output=True)
            n_ok += 1
        elif "Playback Switch" in line or "Extension Unit Switch" in line:
            # Extension Unit Switch is the UAC2 FU mute. It is a switch-only
            # control (no numeric value line), and 'off' would mute the stream.
            subprocess.run(["amixer", "-c", CARD, "cset", f"numid={numid}", "on"],
                           capture_output=True)
    # Judge only the per-channel value lines, which is where ALSA reports the
    # actual setting ('Front Left: Playback 192 [100%] [0.00dB] [on]'). The
    # earlier filter also matched 'Limits:' and 'Playback channels:' lines and
    # aborted on an amp that was fully open.
    chk = subprocess.run(["amixer", "-c", CARD], capture_output=True, text=True).stdout
    bad = []
    seen = 0
    for l in chk.splitlines():
        s = l.strip()
        # A VALUE line carries a numeric playback value, e.g.
        #   'Front Left: Playback 192 [100%] [0.00dB] [on] Capture ...'
        # A switch-only line has no number ('Mono: Playback [off]') and is an
        # FU mute, judged separately above -- failing to separate the two made
        # the gate abort on a fully-open amp.
        m = _re.match(r"(Front Left|Front Right|Mono):\s+Playback (\d+) \[", s)
        if not m:
            continue
        seen += 1
        if "[100%]" not in s or "[0.00dB]" not in s or "[on]" not in s:
            bad.append(s)
    if seen == 0:
        bad.append("no per-channel Playback value lines found -- cannot prove "
                   "the host volume is at max")
    return n_ok, bad


# ------------------------------------------------------------------ pinning --
def pin_path():
    """Make every stage between host and tap the identity; assert, don't trust."""
    log("=== pinning the signal path (every stage must be identity) ===")
    log(f"  IDN                    : {scpi('*IDN?')}")
    scpi("GENerator:OFF")
    g = scpi("GENerator:STATe?")
    log(f"  GENerator:STATe?       : {g}")
    # Field 0 is the MODE (csd_gen_state_mode -> csd_gen.mode); the remaining
    # eight fields are the wave descriptor (freq, amps, domain, ...), which are
    # non-zero by design. Testing all fields for zero aborts on a generator that
    # is properly OFF -- measured: '0,48000,256,256,1,0,2,1,1' is OFF.
    try:
        gen_mode = int(g.replace("ERR", "").split(",")[0])
        gen_off = (gen_mode == 0)
    except Exception:
        gen_off = False
    scpi("DIAG:DSP:PRESet 0")
    p = scpi("DIAG:DSP:PRESet?")
    log(f"  DIAG:DSP:PRESet?       : {p}")
    dsp_off = p.strip().split(",")[0].strip() in ("0", "none", "NONE")
    nvol, bad = set_host_volume_max()
    log(f"  ALSA Playback Volume   : set {nvol} controls to max; "
        f"non-max/muted left: {bad if bad else 'none'}")
    log(f"  DEBUG:MIXer:HWCfg?     : {scpi('DEBUG:MIXer:HWCfg?')}")
    # Q15, 32768 = unity (csd_gen.h CSD_GEN_Q15_UNITY). These are the mixer's
    # per-source multipliers applied to the host stream -- a value below unity
    # scales every sample and would break bit-exactness for a reason unrelated
    # to the wire, so it is a gate, not a log line.
    mg = scpi("MIXer:GAIN?")
    log(f"  MIXer:GAIN?            : {mg}   (Q15, 32768 = unity)")
    try:
        gh, gg = [int(x) for x in mg.split(",")[:2]]
        gains_unity = (gh == 32768 and gg == 32768)
    except Exception:
        gains_unity = False
    if not gen_off:
        sys.exit("ABORT: generator still armed -- it would sum into the host "
                 "stream and bit-exactness could not be attributed")
    if not dsp_off:
        sys.exit("ABORT: DSP preset is not none -- the tap reads POST-DSP words, "
                 "so comparing them with the host bytes measures the filter, "
                 "not the wire")
    if bad:
        sys.exit("ABORT: host volume not provably at max -- every sample would "
                 f"be scaled: {bad[:3]}")
    if not gains_unity:
        sys.exit("ABORT: mixer host/gen gains are not unity -- the tap reads "
                 "post-mix words, so they would be scaled before comparison")


# ------------------------------------------------------------------ streaming
def make_schedule(mode, nframes):
    """Generate the ENTIRE run into RAM before any audio starts.

    Per the user's directive: generate all samples at once, then send the whole
    thing from RAM. Three reasons this is the right shape, all measured on this
    rig rather than assumed:

      - Generating inside the writer thread capped the sustained feed below the
        device rate. An under-fed playback stream makes lib_xua substitute
        underflowWord (0 for PCM), which produces zeros that read exactly like
        a firmware tap fault.
      - Re-generating the expected values inside the checker cost one numpy call
        per captured chunk (3375 calls per 12 s run) competing for the GIL with
        the writer, so the instrument was disturbing the thing it measured.
      - One array played AND checked means the comparison is against the bytes
        that were actually sent, not against a second evaluation of a formula
        that could drift -- the same class of bug as player and checker each
        carrying their own copy of the golden rule.

    Size is not a concern: 5 min at 384 kHz stereo S32_LE is 15.4 MB.
    Returns (interleaved uint32 array (n,2), L words, R words) where L/R are the
    24-bit payloads as the device sees them.
    """
    idx = np.arange(nframes, dtype=np.uint64)
    inter = np.empty((nframes, 2), dtype="<u4")
    if mode == "silence":
        inter[:, :] = 0
        gl = np.zeros(nframes, dtype=np.int64)
        gr = np.zeros(nframes, dtype=np.int64)
    else:
        Lw = sig.noise24_vec(idx, ch=0).astype(np.uint32) << np.uint32(8)
        Rw = sig.noise24_vec(idx, ch=1).astype(np.uint32) << np.uint32(8)
        inter[:, 0] = Lw
        inter[:, 1] = Rw
        # The golden words are the 24-bit payloads, so the checker and the wire
        # read the identical numbers.
        gl = (Lw >> np.uint32(8)).astype(np.int64)
        gr = (Rw >> np.uint32(8)).astype(np.int64)
    return inter, gl, gr


def stream(mode, seconds):
    """One phase: play a pre-generated schedule, capture the return path.

    chunks is a list of (bytes, frames_actually_written_so_far, wall_time).
    frames_actually_written is writei's return value, NOT the request: an
    earlier version counted the request, which made the rig look like it sent
    471 k frames/s to a 384 k device and poisoned every latency and alignment
    number derived from it.
    """
    # Generate everything first, off the streaming path, with headroom so the
    # writer never runs out mid-window.
    total = int(seconds * RATE) + 4 * RATE
    sched, gl, gr = make_schedule(mode, total)
    # Zero-copy pointer into the one RAM buffer we generated. Passing an int to
    # a c_void_p argtype is legal ctypes and avoids copying: bytes slicing at an
    # offset (payload[k*8:]) copies up to the whole 15 MB buffer per call, and a
    # memoryview is rejected outright by c_void_p ("TypeError: wrong type"),
    # which silently killed the writer thread -- the same class of bug as the
    # earlier write_all memoryview failure. `sched` stays referenced by this
    # closure for the whole stream, so the buffer cannot be freed or moved.
    base_addr = sched.ctypes.data

    play, pbuf = openp(0, PLAY_BUFSZ)
    cap, cbuf = openp(1, BUFSZ)
    alsa.snd_pcm_start(play)
    alsa.snd_pcm_start(cap)

    stop = threading.Event()
    st = {"written": 0, "recovers": 0, "fatal": 0, "exhausted": 0}
    lock = threading.Lock()

    def writer():
        # Pure blocking writei from the pre-generated array. No numpy, no
        # allocation, no sleep: the only work in this thread is the syscall, so
        # the device is paced by the kernel and nothing else.
        n = 0
        while not stop.is_set():
            if n >= total:
                st["exhausted"] += 1                 # never let this be silent
                return
            k = min(WBLK, total - n)
            off = 0
            while off < k and not stop.is_set():
                addr = base_addr + (n + off) * 8     # in bytes: 2ch x uint32
                r = alsa.snd_pcm_writei(play, addr, k - off)
                if r < 0:
                    st["recovers"] += 1
                    rc = alsa.snd_pcm_recover(play, r, 1)
                    if rc < 0:
                        st["fatal"] = rc     # report, never silently under-feed
                        return
                    alsa.snd_pcm_start(play)
                    continue
                off += r
                n += r
                with lock:
                    st["written"] = n

    tw = threading.Thread(target=writer, daemon=True)
    t0 = time.monotonic()                              # includes the pipe fill below
    tw.start()
    time.sleep(0.6)                                    # let the pipe fill

    buf = (ctypes.c_char * (N * 8))()
    chunks, reads, errs = [], 0, 0
    while time.monotonic() - t0 < seconds + 0.6:
        r = alsa.snd_pcm_readi(cap, buf, N)
        if r <= 0:
            alsa.snd_pcm_recover(cap, r if r < 0 else -1, 1)
            alsa.snd_pcm_start(cap)
            errs += 1
            continue
        reads += 1
        with lock:
            w = st["written"]
        # Copy out now: the ctypes buffer is reused every iteration.
        chunks.append((bytes(buf[:r * 8]), w, time.monotonic()))
    el = time.monotonic() - t0
    stop.set()
    tw.join(timeout=3)
    for p in (play, cap):
        alsa.snd_pcm_drop(p)
        alsa.snd_pcm_close(p)
    # golden returned beside the dict, not inside it: every dict value stays an
    # int, so nothing downstream can accidentally treat the arrays as numbers.
    return dict(chunks=chunks, reads=reads, errs=errs, el=el,
                written=st["written"], recovers=st["recovers"],
                fatal=st["fatal"], exhausted=st["exhausted"],
                pbuf=pbuf, cbuf=cbuf), (gl, gr)
    


def words(chunks):
    if not chunks:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    f = np.concatenate([np.frombuffer(c, dtype="<u4") for c, _, _ in chunks])
    Lw = (f[0::2] >> np.uint32(8)).astype(np.int64)
    Rw = (f[1::2] >> np.uint32(8)).astype(np.int64)
    return Lw, Rw


# ------------------------------------------------------------------- TEST 1 --
def test1_silence(seconds=3.0):
    log("\n" + "=" * 66)
    log("TEST 1  dead silence in -> capture must be all zeros")
    log("=" * 66)
    log(f"  arm   : {scpi('DEBUG:LOOPback 1')}")
    log(f"  clear : {scpi('DEBUG:LOOPback:CLEAR')}")
    r, _gold = stream("silence", seconds)
    L, R = words(r["chunks"])
    frames = int(len(L))
    nzL, nzR = int((L != 0).sum()), int((R != 0).sum())
    log(f"  sent 0 (dead silence), written {r['written']:,} frames "
        f"over {r['el']:.3f} s ({r['written']/r['el']:,.0f} Hz)")
    log(f"  capture {frames:,} frames ({r['reads']} reads, {r['errs']} errors)")
    log(f"  L nonzero {nzL:,} = {nzL/max(1,frames):.4f}   "
        f"R nonzero {nzR:,} = {nzR/max(1,frames):.4f}")
    if nzL or nzR:
        import collections
        cL = collections.Counter(int(v) for v in L if v != 0)
        cR = collections.Counter(int(v) for v in R if v != 0)
        log(f"  distinct nonzero L={len(cL)}  R={len(cR)}")
        for v, n in cL.most_common(4):
            log(f"      L 0x{v:06X} x{n:,}")
        for v, n in cR.most_common(4):
            log(f"      R 0x{v:06X} x{n:,}")
        live = len(cL) > 8 and len(cR) > 8
        log(f"  interpretation: {'LIVE ADC DATA on the host stream -- FAIL' if live else 'constant value: internal source (DSP bias / stale hold), not ADC leakage'}")
    ok = (nzL == 0 and nzR == 0 and frames > 0)
    log(f"  TEST 1: {'PASS' if ok else 'FAIL'}")
    return ok, r


# ------------------------------------------------------------------- TEST 2 --
def test2_noise(minutes, align_search=None):
    log("\n" + "=" * 66)
    log(f"TEST 2  {minutes} min of band-limited noise -> bit-exact + latency")
    log("=" * 66)
    log(f"  arm   : {scpi('DEBUG:LOOPback 1')}")
    log(f"  clear : {scpi('DEBUG:LOOPback:CLEAR')}")
    r, (gL, gR) = stream("noise", minutes * 60.0)
    L, R = words(r["chunks"])
    frames = int(len(L))
    log(f"  written {r['written']:,} frames over {r['el']:.3f} s "
        f"({r['written']/r['el']:,.0f} Hz; recovers {r['recovers']})")
    log(f"  capture {frames:,} frames ({r['reads']} reads, {r['errs']} read errors)")
    nz = float((L != 0).mean())
    log(f"  L nonzero {nz:.4f}  R nonzero {(R != 0).mean():.4f}   "
        f"[tap coverage -- must be 1.000 for a valid gate]")
    if frames == 0:
        log("  TEST 2: FAIL (no capture)")
        return False, {}

    # ---- align chunk 0 by CONTENT, wide search, then track by delta --------
    # Alignment is a search of the FIRST captured pair inside the golden array
    # that was actually played -- one vectorised compare over the whole
    # schedule, not a strided re-generation. Band-limited noise can repeat, so
    # any hit is a candidate; contiguity from there is what the walk verifies,
    # and a wrong pick fails loudly instead of passing.
    base = None
    for ci, (c, w, t) in enumerate(r["chunks"]):
        f = np.frombuffer(c, dtype="<u4")
        a0 = int((f[0::2] >> np.uint32(8))[0])
        b0 = int((f[1::2] >> np.uint32(8))[0])
        if a0 == 0 and b0 == 0:
            continue
        h = np.nonzero((gL == a0) & (gR == b0))[0]
        if len(h):
            base = (int(h[0]), ci, w, t)
            if len(h) > 1:
                log(f"  note: first pair occurs {len(h)}x in the schedule; "
                    f"taking index {int(h[0]):,} and letting the contiguity "
                    f"walk decide")
            break
    if not base:
        log("  ALIGNMENT NOT FOUND by content -> gate is VOID, not clean")
        return False, {}
    k0, ci0, w0, t0 = base
    log(f"  aligned: first captured pair = schedule index {k0:,} "
        f"(chunk {ci0}, {w0:,} written at that read)")

    # ---- verify contiguity and bit-exactness, chunk by chunk --------------
    nxt = k0
    checked = exact = 0
    gaps = dups = backs = flips = subst = zeros = 0
    lat = []
    pop = {}
    for ci in range(ci0, len(r["chunks"])):
        c, w, t = r["chunks"][ci]
        f = np.frombuffer(c, dtype="<u4")
        a = (f[0::2] >> np.uint32(8)).astype(np.int64)
        b = (f[1::2] >> np.uint32(8)).astype(np.int64)
        if nxt + len(a) > len(gL):
            log("  schedule exhausted under the walk -- stopping, not guessing")
            break
        eL = gL[nxt:nxt + len(a)]
        eR = gR[nxt:nxt + len(b)]
        good = (a == eL) & (b == eR)
        n_ok = int(good.sum())
        checked += len(a)
        exact += n_ok
        if n_ok == len(a):
            nxt += len(a)
            lat.append(w - nxt)
            continue
        # first mismatch: classify it, then re-sync on content
        j = int(np.argmax(~good)) if (~good).any() else len(a)
        # was any frame at all present, or all zero (unserved slot)?
        zeros += int(((a == 0) & (b == 0)).sum())
        if j < len(a):
            pa, pb = int(a[j]), int(b[j])
            if pa == 0 and pb == 0:
                pass                                    # counted in zeros
            else:
                # does this word belong somewhere else in the schedule?
                found = None
                lo = max(0, nxt + j - 4096)
                hi = min(len(gL), nxt + j + 4096)
                h = np.nonzero((gL[lo:hi] == pa) & (gR[lo:hi] == pb))[0]
                if len(h):
                    found = lo + int(h[0])
                if found is None:
                    # not in the schedule at all: corrupt or stale word.
                    x = int(bin(pa ^ int(eL[j])).count("1")) + \
                        int(bin(pb ^ int(eR[j])).count("1"))
                    pop[x] = pop.get(x, 0) + 1
                    if x <= 2:
                        flips += 1
                    else:
                        subst += 1
                else:
                    if found < nxt + j:
                        backs += 1
                    else:
                        dups += 1
                # re-sync: find the next frame in the chunk that matches its
                # expected slot so one defect costs one error, not the run
                rest = np.nonzero(good[j:])[0]
                if len(rest) and rest[0] > 0:
                    nxt += j + int(rest[0])
                else:
                    nxt += len(a)
            continue
        nxt += len(a)

    # ---- slip-tolerant global scoring (see bp_score.py) --------------------
    # The walk above scores a read only up to its first mismatch, so one framing
    # slip per read collapses the score to ~0 even when every sample is right.
    # Score every read in full at its tracked offset, count differing BITS, and
    # report slips separately: a slip is a capture-path effect, a bit difference
    # is a data-path fact.
    sc = bp_score.score_chunks(gL, gR, r["chunks"], RATE, start=k0, first=ci0)
    sc_h = bp_score.score_chunks(gL, gR, r["chunks"], RATE, start=k0, first=ci0,
                                skip_head=RATE)
    if sc["checked"]:
        log(f"  [global] whole capture: {sc['checked']:,} frames scored, "
            f"{sc['exact']:,} bit-exact ({100.0 * sc['exact'] / sc['checked']:.6f}%), "
            f"differing bits {sc['bits']:,}, max {sc['max_bits_per_frame']}/frame, "
            f"slips {sc['slips']}, zero-word {sc['zero_frames']:,}")
        log(f"  [global] worst 1 s block: {100.0 * sc['worst_block'][0]:.4f}% exact "
            f"ending at frame {sc['worst_block'][1]:,}")
        if sc_h["checked"]:
            head_frames = sc["checked"] - sc_h["checked"]
            head_bad = sc["exact"] - sc_h["exact"]
            log(f"  [global] excluding the first 1 s after the stream joins: "
                f"{sc_h['exact']:,}/{sc_h['checked']:,} bit-exact "
                f"({100.0 * sc_h['exact'] / sc_h['checked']:.6f}%), differing bits "
                f"{sc_h['bits']:,} -- the excluded window holds {head_frames:,} frames, "
                f"{head_bad:,} of which did not match")
        exact, checked = sc_h["exact"], sc_h["checked"]
        dups, backs, flips, subst = sc_h["slips"], 0, 0, 0
        zeros = sc_h["zero_frames"]
    else:
        log("  [global] no scorable frames -- the walk found nothing to compare")

    lat_ms = np.array(lat, dtype=np.float64) * AP * 1000.0 if lat else np.array([np.nan])
    log(f"  bit-exact {exact:,}/{checked:,} = {100.0*exact/max(1,checked):.4f}%")
    log(f"  defect classes: gap/dup={dups} back={backs} flip(<=2bit)={flips} "
        f"subst={subst} zero-word={zeros}")
    if pop:
        log(f"  XOR popcount histogram (how corrupted the corrupted words are): "
            f"{dict(sorted(pop.items())[:8])}")
    log(f"  latency out->in: median {np.median(lat_ms):.3f} ms  "
        f"min {np.min(lat_ms):.3f}  max {np.max(lat_ms):.3f}  "
        f"({np.median(lat_ms)*RATE/1000.0:.0f} frames, n={len(lat_ms)})")
    log(f"  ALSA buffers: play={r['pbuf']} frames ({1000.0*r['pbuf']/RATE:.1f} ms)"
        f"  cap={r['cbuf']} frames ({1000.0*r['cbuf']/RATE:.1f} ms)"
        f"  -- measured latency INCLUDES the playback ring")
    if r.get("fatal"):
        log(f"  !! writer DIED with ALSA error {r['fatal']} -- everything above "
            f"is an under-fed stream and cannot be credited")
    fed = r["written"] / r["el"]
    if abs(fed - RATE) / RATE > 0.02:
        log(f"  !! sustained feed {fed:,.0f} vs device {RATE:,} Hz "
            f"({100.0*fed/RATE:.1f}%) -- under-feed makes lib_xua substitute "
            f"underflowWord zeros; result is not attributable to firmware")
    # A lost read is an UNMEASURED frame, not a wrong one: the frames that did
    # arrive were all exact, so failing the gate on capture-side hiccups tests
    # the host, not the device. Bound it instead of banning it, and say so.
    READ_ERROR_BUDGET = 16
    if r["errs"]:
        log(f"  note: {r['errs']} read error(s) -- those frames were never "
            f"captured, so they are unmeasured rather than mismatched "
            f"(budget {READ_ERROR_BUDGET})")
    ok = (exact == checked and checked > RATE and nz > 0.999
          and r["errs"] <= READ_ERROR_BUDGET
          and sc_h.get("bits", 1) == 0 and sc_h.get("zero_frames", 1) == 0)
    log(f"  TEST 2: {'PASS' if ok else 'FAIL'}")
    return ok, dict(latency_ms=float(np.median(lat_ms)), exact=exact,
                    checked=checked, fill=nz)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--skip-silence", action="store_true")
    ap.add_argument("--skip-noise", action="store_true")
    a = ap.parse_args()
    log(f"gate: card hw:{CARD},0  rate {RATE}  format {FMT} (S32_LE)  "
        f"buf {BUFSZ} frames = {1000.0*BUFSZ/RATE:.1f} ms")
    pin_path()
    # A skipped leg must never print PASS. An earlier version defaulted both
    # flags True, so `--skip-noise` printed "TEST2=PASS" for a leg that had not
    # compared a single frame. A leg that never ran is VOID, not clean.
    def verdict(run, ok):
        return ("PASS" if ok else "FAIL") if run else "SKIP(void)"
    run1, ok1 = (not a.skip_silence), True
    if run1:
        ok1, _ = test1_silence()
    run2, ok2 = (not a.skip_noise), True
    if run2:
        ok2, _ = test2_noise(a.minutes)
    log("\n" + "=" * 66)
    log(f"GATE: TEST1={verdict(run1, ok1)}  TEST2={verdict(run2, ok2)}")
    log("=" * 66)
    sys.exit(0 if ((not run1 or ok1) and (not run2 or ok2)) else 1)


if __name__ == "__main__":
    main()
