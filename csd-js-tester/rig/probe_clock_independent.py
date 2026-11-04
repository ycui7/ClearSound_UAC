#!/usr/bin/env python3
"""Independent proof of the real I2S sample rate, plus tap fill and alignment.

Why: two earlier probes reported device rates from firmware counters or from the
reader loop and both were rig artefacts. This probe measures the rate with two
things that do not involve our counters at all:

  1. The ES9219Q DPLL lock bit (reg 0x4D bit1), read over our own SCPI
     REGister:READ? path. The DPLL locks to the BCLK the XMOS drives. If it is
     LOCKED while ALSA says 384 kHz, the I2S bus really is clocked at the rate
     the host negotiated -- a counter saying otherwise is a counter bug, not a
     bus failure. If it is UNLOCKED, the bus is not running as negotiated and
     nothing downstream can be certified.

  2. The pitch of a known tone measured through the tap. Play a 1 kHz sine at
     384 kHz; if the tap delivers it back at 1 kHz, the I2S loop consumes at
     exactly the USB rate. A peak at 585 Hz would mean the I2S loop runs at
     0.585x -- the ratio the tap put() counter suggested. Pitch is a property of
     the samples themselves and cannot be produced by a miscounted counter.

  3. Fill / bit-exactness on noise, with the alignment search ANCHORED to the
     number of frames we have sent. The previous version scanned schedule
     indices 0..2M, but the stream was already ~3M frames deep, so it could not
     find its own data and reported "aligned: None" -- a rig bug, not a device
     fault.

Fixes carried in from that lesson: collection is a tight loop with no analysis,
and writes are paced by a small ALSA buffer so we cannot run ahead of the
device.
"""
import ctypes, os, subprocess, sys, threading, time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import csd_signal as sig

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
CARD = os.environ.get("CSD_CARD", "3")
RATE = sig.RATE
WINDOW = 4.0
# Chunk must fit inside the ALSA buffer or avail never reaches it and the
# writer paces forever without sending a single frame (that is how "sent 0"
# happened with N=4800 against a 2048-frame buffer).
N = 1024
# ALSA buffer size is expressed in FRAMES, not bytes. 2048 frames at 384 kHz is
# 5.3 ms of audio, so the host can never run more than 5.3 ms ahead of the
# device -- which is what makes any latency or fill measurement meaningful.
# ALSA buffer size is in FRAMES. Default 2048 = 5.3 ms at 384 kHz, which keeps
# latency measurements honest. Overridable for ONE purpose only: the slack A/B
# that separates "host/USB delivered too late" from "firmware could not keep up"
# -- with a big buffer the host is never the limiter, so any remaining zeros are
# firmware-side. When overridden, the run is NOT a latency measurement and the
# probe says so.
BUFSZ = int(os.environ.get("CSD_BUFSZ", "2048"))


def scpi(cmd):
    r = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                        "--timeout", "15", cmd], cwd=APP,
                       capture_output=True, text=True, timeout=40)
    return (r.stdout or "").strip().replace("\r", "")


def codec_reg(reg):
    """Read an ES9219Q register through the firmware's own I2C path.

    Payload is base64: libscpi's lexer rejects several hex spellings on this
    device (0x03 -> -101 while 0x3 works), so the command form depends on the
    number of digits unless the bytes go over as one opaque token. The client
    in csd_diag.py does the same; returning the RAW reply here means a rejected
    command shows as an empty string -- an earlier version printed that empty
    string and the leg read as 'no data' rather than 'bad syntax'.
    """
    import base64
    payload = base64.b64encode(bytes([reg])).decode()
    reply = scpi(f'REGister:READ? "{payload}"')
    # reply may carry more than one quoted field; take the last, per the
    # documented trap, and decode it as b64 bytes.
    import re as _re
    q = _re.findall(r'"([^"]*)"', reply) or [reply]
    try:
        raw = base64.b64decode(q[-1], validate=True)
    except Exception:
        return f"UNPARSED reply={reply!r}"
    return " ".join(f"0x{b:02X}" for b in raw)


print("IDN:", scpi("*IDN?"))
alsa = ctypes.CDLL("libasound.so.2")
for f_, r_, a_ in (("snd_pcm_format_name", ctypes.c_char_p, [ctypes.c_int]),
                   ("snd_pcm_writei", ctypes.c_long,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
                   ("snd_pcm_readi", ctypes.c_long,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
                   ("snd_pcm_avail_update", ctypes.c_long, [ctypes.c_void_p]),
                   ("snd_pcm_recover", ctypes.c_int,
                    [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]),
                   ("snd_pcm_hw_params_set_buffer_size_near", ctypes.c_int,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]),
                   ("snd_pcm_hw_params_set_period_size_near", ctypes.c_int,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint),
                     ctypes.POINTER(ctypes.c_int)])):
    getattr(alsa, f_).restype = r_
    getattr(alsa, f_).argtypes = a_


def resolve(want):
    for v in range(64):
        nm = alsa.snd_pcm_format_name(v)
        if nm is None:
            continue
        nb = nm if isinstance(nm, bytes) else nm.encode()
        if nb.decode().upper() == want.upper():
            return v
    sys.exit(f"no format {want}")


FMT = resolve("S32_LE")


def openp(stream):
    pcm = ctypes.c_void_p()
    if alsa.snd_pcm_open(ctypes.byref(pcm), f"hw:{CARD},0".encode(), stream, 0) < 0:
        sys.exit(f"open stream {stream} failed")
    hp = ctypes.c_void_p()
    alsa.snd_pcm_hw_params_malloc(ctypes.byref(hp))
    alsa.snd_pcm_hw_params_any(pcm, hp)
    for fn, arg in ((alsa.snd_pcm_hw_params_set_access, 3),
                    (alsa.snd_pcm_hw_params_set_format, FMT),
                    (alsa.snd_pcm_hw_params_set_channels, 2)):
        if fn(pcm, hp, arg) < 0:
            sys.exit(f"stream {stream} rejected param")
    d = ctypes.c_uint(RATE)
    alsa.snd_pcm_hw_params_set_rate_near(pcm, hp, ctypes.byref(d), 0)
    if d.value != RATE:
        sys.exit(f"negotiated {d.value}")
    # These two calls MUST have argtypes registered or ctypes passes them wrong
    # and ALSA silently keeps its own default; an earlier version printed a
    # buffer size it had initialised itself, which hid a writer running 5 s
    # ahead of a 384 kHz device.
    b = ctypes.c_uint(BUFSZ)
    if alsa.snd_pcm_hw_params_set_buffer_size_near(pcm, hp, ctypes.byref(b)) < 0:
        sys.exit(f"stream {stream}: buffer size request rejected")
    if b.value > RATE // 50:                      # more than 20 ms of slack
        if os.environ.get("CSD_BUFSZ"):
            print(f"  !! stream {stream}: buffer {b.value} frames = "
                  f"{1000.0*b.value/RATE:.1f} ms -- DELIBERATE slack A/B. "
                  f"Latency numbers from this run are void.")
        else:
            sys.exit(f"stream {stream}: granted buffer {b.value} frames = "
                     f"{1000.0*b.value/RATE:.1f} ms -- too much slack, the writer "
                     f"could run ahead of the device and poison timing results")
    p = ctypes.c_uint(1024)
    if alsa.snd_pcm_hw_params_set_period_size_near(pcm, hp, ctypes.byref(p), None) < 0:
        sys.exit(f"stream {stream}: period size request rejected")
    if alsa.snd_pcm_hw_params(pcm, hp) < 0:
        sys.exit(f"stream {stream} hw_params failed")
    print(f"  stream {stream}: buffer={b.value} period={p.value}")
    alsa.snd_pcm_prepare(pcm)
    return pcm


def tone24_vec(idx, freq, ch):
    return sig.tone24_vec(idx, freq=freq, ch=ch)


def run(mode, f_tone=1000.0):
    """Stream `mode` for WINDOW s, collect raw capture, return everything."""
    stop = threading.Event()
    w = {"sent": 0, "rec": 0}
    chunks = []                      # (bytes, frames_sent_so_far)

    def writer():
        n = 0
        while not stop.is_set():
            av = alsa.snd_pcm_avail_update(play)
            if av < 0:
                alsa.snd_pcm_recover(play, av, 1)
                alsa.snd_pcm_start(play)
                continue
            if av < N:
                time.sleep(0.001)
                continue
            k = min(av, N)
            idx = np.arange(n, n + k, dtype=np.uint64)
            inter = np.empty((k, 2), dtype="<u4")
            if mode == "silence":
                inter[:, :] = 0
            elif mode == "tone":
                inter[:, 0] = tone24_vec(idx, freq=f_tone, ch=0).astype(np.uint32) << np.uint32(8)
                inter[:, 1] = tone24_vec(idx, freq=f_tone, ch=1).astype(np.uint32) << np.uint32(8)
            else:
                inter[:, 0] = sig.noise24_vec(idx, ch=0).astype(np.uint32) << np.uint32(8)
                inter[:, 1] = sig.noise24_vec(idx, ch=1).astype(np.uint32) << np.uint32(8)
            r = alsa.snd_pcm_writei(play, inter.tobytes(), k)
            if r < 0:
                w["rec"] += 1
                alsa.snd_pcm_recover(play, r, 1)
                alsa.snd_pcm_start(play)
                continue
            w["sent"] += k
            n += k

    play, cap = openp(0), openp(1)
    alsa.snd_pcm_start(play)
    alsa.snd_pcm_start(cap)
    tw = threading.Thread(target=writer, daemon=True)
    tw.start()
    time.sleep(0.75)                                     # let the pipe fill
    reg_before = codec_reg(0x4D)
    am_before = codec_reg(0x48)
    tx0 = scpi("DEBUG:LOOPback?")

    buf = (ctypes.c_char * (N * 8))()
    err = reads = 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < WINDOW:
        r = alsa.snd_pcm_readi(cap, buf, N)
        if r <= 0:
            alsa.snd_pcm_recover(cap, r if r < 0 else -1, 1)
            alsa.snd_pcm_start(cap)
            err += 1
            continue
        reads += 1
        chunks.append((bytes(buf[:r * 8]), w["sent"]))   # tight loop, no analysis
    el = time.monotonic() - t0
    reg_after = codec_reg(0x4D)
    am_after = codec_reg(0x48)
    tx1 = scpi("DEBUG:LOOPback?")
    stop.set()
    tw.join(timeout=3)
    alsa.snd_pcm_drop(play)
    alsa.snd_pcm_drop(cap)
    alsa.snd_pcm_close(play)
    alsa.snd_pcm_close(cap)

    def cnt(t):
        try:
            return int(t.split(",")[1])
        except Exception:
            return 0
    return dict(mode=mode, el=el, sent=w["sent"], rec=w["rec"], err=err,
                reads=reads, chunks=chunks, reg0=reg_before, reg1=reg_after,
                am0=am_before, am1=am_after, puts0=cnt(tx0), puts1=cnt(tx1),
                state=scpi("DEBUG:LOOPback?"))


def analyse(res):
    chunks = res["chunks"]
    frames = sum(len(c) // 8 for c, _ in chunks)
    print(f"\n--- {res['mode']} over {res['el']:.3f} s ---")
    print(f"  sent {res['sent']:,} = {res['sent']/res['el']:,.0f} Hz "
          f"(recovered {res['rec']})")
    print(f"  capture {frames:,} = {frames/res['el']:,.0f} Hz "
          f"({res['reads']} reads, {res['err']} errors)")
    puts = res["puts1"] - res["puts0"]
    print(f"  tap puts {puts:,} = {puts/res['el']:,.0f} /s "
          f"= {puts/res['el']/RATE:.3f} x frame-rate, {puts/res['el']/RATE/2:.3f}/ch/frame")
    print(f"  codec 0x4D (DPLL/ASRC)  before={res['reg0']}  after={res['reg1']}")
    print(f"  codec 0x48 (OC/automute) before={res['am0']}  after={res['am1']}")
    if res["err"] > 5:
        print(f"  {res['err']} READ ERRORS -> capture rate invalid")
    if frames == 0:
        return None

    Lw_all, Rw_all, sent_at = [], [], []
    for c, s in chunks:
        f = np.frombuffer(c, dtype="<u4")
        Lw_all.append((f[0::2] >> np.uint32(8)).astype(np.int64))
        Rw_all.append((f[1::2] >> np.uint32(8)).astype(np.int64))
        sent_at.append(s)
    Lw = np.concatenate(Lw_all)
    Rw = np.concatenate(Rw_all)
    nz = float((Lw != 0).mean())
    print(f"  L nonzero {nz:.4f}  R nonzero {(Rw != 0).mean():.4f}")

    if res["mode"] == "silence":
        print(f"  nonzero words in silence: L={int((Lw!=0).sum()):,} "
              f"R={int((Rw!=0).sum()):,}  distinct L={len(set(Lw[Lw!=0]))}")
        return None
    if res["mode"] == "tone":
        x = (Lw - Lw.mean()).astype(np.float64)
        if not np.any(x):
            print("  tone: all zero -> VOID")
            return None
        X = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        k = int(np.argmax(X))
        peak = k * res["el"] and k / (len(x) / RATE)
        print(f"  tone peak {peak:,.1f} Hz  (sent 1000 Hz)  "
              f"ratio {peak/1000.0:.4f}")
        print(f"  ratio ~1.000 -> I2S consumes at the USB rate (independent of "
              "counters)")
        print(f"  ratio ~0.585 -> I2S loop runs at 0.585x, matching the put() "
              "counter")
        return peak

    # noise: content alignment, anchored at how many frames we had sent
    first = None
    for j, (c, s) in enumerate(chunks):
        f = np.frombuffer(c, dtype="<u4")
        a = int((f[0::2] >> np.uint32(8))[0])
        b = int((f[1::2] >> np.uint32(8))[0])
        if a == 0 and b == 0:
            continue
        lo = max(0, s - 6 * RATE)
        for start in range(lo, lo + 12 * RATE, 1 << 16):
            idx = np.arange(start, start + (1 << 16), dtype=np.uint64)
            eL = sig.noise24_vec(idx, ch=0).astype(np.int64)
            eR = sig.noise24_vec(idx, ch=1).astype(np.int64)
            h = np.nonzero((eL == a) & (eR == b))[0]
            if len(h):
                first = (start + int(h[0]), j, s)
                break
        if first:
            break
    if not first:
        print("  ALIGNMENT NOT FOUND in window around send-count -> this probe "
              "cannot judge; not a device verdict")
        return None
    base, cj, sent = first
    print(f"  aligned: first captured pair = schedule index {base:,} "
          f"(chunk {cj}, {sent:,} sent at that read)")
    k0 = 0
    ok = checked = 0
    for c, s in chunks[cj:]:
        f = np.frombuffer(c, dtype="<u4")
        a = (f[0::2] >> np.uint32(8)).astype(np.int64)
        b = (f[1::2] >> np.uint32(8)).astype(np.int64)
        idx = np.arange(base + k0, base + k0 + len(a), dtype=np.uint64)
        eL = sig.noise24_vec(idx, ch=0).astype(np.int64)
        eR = sig.noise24_vec(idx, ch=1).astype(np.int64)
        m = (a == eL) & (b == eR)
        ok += int(m.sum())
        checked += len(a)
        k0 += len(a)
    print(f"  bit-exact {ok:,}/{checked:,} = {100.0*ok/max(1,checked):.4f}%")
    return None


print("\n" + "=" * 62)
print("LEG 0  silence: what does the tap return when we send nothing?")
print("=" * 62)
print("arm:", scpi("DEBUG:LOOPback 1"))
scpi("DEBUG:LOOPback:CLEAR")
analyse(run("silence"))

print("\n" + "=" * 62)
print("LEG 1  1 kHz tone: pitch is an independent clock reference")
print("=" * 62)
analyse(run("tone"))

print("\n" + "=" * 62)
print("LEG 2  noise: fill, alignment, bit-exactness")
print("=" * 62)
analyse(run("noise"))
print("\nstate:", scpi("DEBUG:LOOPback?"))
