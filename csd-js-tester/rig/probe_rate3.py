#!/usr/bin/env python3
"""Measure the loopback tap against an INDEPENDENT clock reference.

Why this exists: an earlier version of this probe reported "capture 79 kHz" and
"LRCLK 224 kHz" and both were artefacts of the probe, not the device.

  - The alignment search (a schedule scan per chunk) ran INSIDE the read loop,
    so the reader starved and capture rate measured the reader, not the device.
    Here collection is a tight loop and analysis happens afterwards.
  - LRCLK was inferred as tap_puts/2, but I have observed that counter
    incrementing while the tap was disarmed even though its code guards it --
    so its per-frame semantics are not established. A firmware timing claim must
    not rest on a counter with unknown semantics.

So the rate is anchored to something the kernel measures for its own reasons:
the USB AUDIO ASYNC feedback endpoint value, exposed as "Momentary freq" in
/proc/asound/cardN/stream0. The device computes that from its own consumption
clock, and the host uses it to meter sends -- if it says 384 kHz, the device
consumes at 384 kHz, independent of anything in our firmware counters.

Everything reported is paired with what it actually measures:
  feedback freq  -> device consumption clock (kernel-visible, independent)
  sent rate      -> what we pushed, paced
  capture rate   -> what came back (only valid if the reader kept up: check
                    read errors and that sent ~= capture over the window)
  tap puts       -> put() call rate (semantics: UNVERIFIED, reported not used)
  fill / exact   -> is the compared data the stimulus at all

Stimulus: stereo band-limited noise, absolute frame index. Never DC -- a
constant carries no ordering information, so replay/drop/duplicate/reorder are
invisible in it.
"""
import ctypes, os, re, subprocess, sys, threading, time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import csd_signal as sig

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
CARD = os.environ.get("CSD_CARD", "3")
RATE = sig.RATE
WINDOW = 6.0
N = 4800
PROC = f"/proc/asound/card{CARD}/stream0"


def scpi(cmd):
    r = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                        "--timeout", "15", cmd], cwd=APP,
                       capture_output=True, text=True, timeout=40)
    return (r.stdout or "").strip().replace("\r", "")


def feedback_hz():
    """The device's own consumption rate as published on the feedback endpoint.
    Independent of every counter we added."""
    try:
        txt = open(PROC).read()
    except OSError:
        return None
    m = re.search(r"Momentary freq = (\d+) Hz", txt)
    if not m:
        return None
    a = re.search(r"Altset = (\d+)", txt)
    f = re.search(r"Bits: (\d+)", txt)
    return int(m.group(1)), a.group(1) if a else "?", f.group(1) if f else "?"


print("IDN:", scpi("*IDN?"))
alsa = ctypes.CDLL("libasound.so.2")
for f_, r_, a_ in (("snd_pcm_format_name", ctypes.c_char_p, [ctypes.c_int]),
                   ("snd_strerror", ctypes.c_char_p, [ctypes.c_int]),
                   ("snd_pcm_state", ctypes.c_int, [ctypes.c_void_p]),
                   ("snd_pcm_avail_update", ctypes.c_long, [ctypes.c_void_p]),
                   ("snd_pcm_writei", ctypes.c_long,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
                   ("snd_pcm_readi", ctypes.c_long,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong])):
    getattr(alsa, f_).restype = r_
    getattr(alsa, f_).argtypes = a_


def resolve(want):
    for v in range(64):
        nm = alsa.snd_pcm_format_name(v)
        if nm is None:
            continue
        nb = nm if isinstance(nm, bytes) else nm.encode()
        if nb.decode().upper() == want.upper():
            if alsa.snd_pcm_format_value(nb) != v:
                sys.exit("format resolver disagrees")
            return v
    sys.exit(f"no format {want}")


FMT = resolve("S32_LE")


def openp(stream):
    pcm = ctypes.c_void_p()
    if alsa.snd_pcm_open(ctypes.byref(pcm), f"hw:{CARD},0".encode(),
                         stream, 0) < 0:
        sys.exit(f"open stream {stream} failed")
    hp = ctypes.c_void_p()
    alsa.snd_pcm_hw_params_malloc(ctypes.byref(hp))
    alsa.snd_pcm_hw_params_any(pcm, hp)
    for fn, arg in ((alsa.snd_pcm_hw_params_set_access, 3),
                    (alsa.snd_pcm_hw_params_set_format, FMT),
                    (alsa.snd_pcm_hw_params_set_channels, 2)):
        if fn(pcm, hp, arg) < 0:
            sys.exit(f"stream {stream} rejected {fn.__name__}")
    d = ctypes.c_uint(RATE)
    alsa.snd_pcm_hw_params_set_rate_near(pcm, hp, ctypes.byref(d), 0)
    if d.value != RATE:
        sys.exit(f"negotiated {d.value} not {RATE}")
    if alsa.snd_pcm_hw_params(pcm, hp) < 0:
        sys.exit(f"stream {stream} hw_params failed")
    alsa.snd_pcm_prepare(pcm)
    return pcm


print("arm  :", scpi("DEBUG:LOOPback 1"))
print("clear:", scpi("DEBUG:LOOPback:CLEAR"))
print("state:", scpi("DEBUG:LOOPback?"))

play, cap = openp(0), openp(1)
alsa.snd_pcm_start(play)
alsa.snd_pcm_start(cap)

stop = threading.Event()
w = {"sent": 0, "rec": 0, "paced": 0}
chunks = []           # RAW capture bytes; analysed after the window


def writer():
    n = 0
    while not stop.is_set():
        av = alsa.snd_pcm_avail_update(play)
        if av < 0:
            alsa.snd_pcm_recover(play, av, 1)
            alsa.snd_pcm_start(play)
            continue
        if av < N:
            w["paced"] += 1
            time.sleep(0.001)
            continue
        k = min(av, N)
        idx = np.arange(n, n + k, dtype=np.uint64)
        L = sig.noise24_vec(idx, ch=0)
        R = sig.noise24_vec(idx, ch=1)
        inter = np.empty((k, 2), dtype="<u4")
        inter[:, 0] = L.astype(np.uint32) << np.uint32(8)
        inter[:, 1] = R.astype(np.uint32) << np.uint32(8)
        r = alsa.snd_pcm_writei(play, inter.tobytes(), k)
        if r < 0:
            w["rec"] += 1
            alsa.snd_pcm_recover(play, r, 1)
            continue
        w["sent"] += k
        n += k


tw = threading.Thread(target=writer, daemon=True)
tw.start()
time.sleep(0.5)
fb0 = feedback_hz()

buf = (ctypes.c_char * (N * 8))()
err = 0
reads = 0
t0 = time.monotonic()
while time.monotonic() - t0 < WINDOW:
    r = alsa.snd_pcm_readi(cap, buf, N)
    if r <= 0:
        alsa.snd_pcm_recover(cap, r if r < 0 else -1, 1)
        alsa.snd_pcm_start(cap)
        err += 1
        continue
    reads += 1
    chunks.append(bytes(buf[:r * 8]))       # no analysis here: keep the loop hot
el = time.monotonic() - t0
fb1 = feedback_hz()
tx = scpi("DEBUG:LOOPback?")
stop.set()
tw.join(timeout=2)
fb2 = feedback_hz()

frames = sum(len(c) // 8 for c in chunks)
try:
    puts = int(tx.split(",")[1])
except Exception:
    puts = 0

# ---------------- analysis, after collection -------------------------------
align = None
base = 0
match = checked = 0
nzL = nzR = 0
pos = 0
for c in chunks:
    f = np.frombuffer(c, dtype="<u4")
    Lw = (f[0::2] >> np.uint32(8)).astype(np.int64)
    Rw = (f[1::2] >> np.uint32(8)).astype(np.int64)
    nzL += int((Lw != 0).sum())
    nzR += int((Rw != 0).sum())
    if align is None:
        # find this chunk's first pair in the schedule by CONTENT once
        if len(Lw) < 8 or (Lw[0] == 0 and Rw[0] == 0):
            pos += len(Lw)
            continue
        for lo in range(0, 1 << 21, 1 << 16):
            idx = np.arange(lo, lo + (1 << 16), dtype=np.uint64)
            eL = sig.noise24_vec(idx, ch=0).astype(np.int64)
            eR = sig.noise24_vec(idx, ch=1).astype(np.int64)
            h = np.nonzero((eL == Lw[0]) & (eR == Rw[0]))[0]
            if len(h):
                align = lo + int(h[0])
                base = pos
                break
        if align is None:
            pos += len(Lw)
            continue
    k0 = pos - base
    idx = np.arange(align + k0, align + k0 + len(Lw), dtype=np.uint64)
    eL = sig.noise24_vec(idx, ch=0).astype(np.int64)
    eR = sig.noise24_vec(idx, ch=1).astype(np.int64)
    ok = (Lw == eL) & (Rw == eR)
    checked += len(Lw)
    match += int(ok.sum())
    pos += len(Lw)


def show(tag, v):
    print(f"  {tag:34s} {v}")


print(f"\n=== RATES over {el:.3f} s ===")
fb = fb2 or fb1 or fb0
show("device feedback freq (kernel)",
     f"{fb[0]:,} Hz  (altset {fb[1]}, {fb[2]} bits)" if fb else "unavailable")
show("we sent (paced)", f"{w['sent']:,} frames = {w['sent']/el:,.0f} Hz "
                       f"(recovers {w['rec']}, paced {w['paced']})")
show("capture read", f"{frames:,} frames = {frames/el:,.0f} Hz "
                     f"({reads} reads, {err} errors)")
show("tap put() calls", f"{puts:,} = {puts/el:,.0f} /s  "
                        f"[semantics UNVERIFIED]")

reader_ok = err == 0 and reads > 0
if not reader_ok:
    print("\n  READER DID NOT KEEP UP -- capture rate is invalid, do not read")
    print("  device behaviour from it.")

print("\n=== IS THE COMPARED DATA THE STIMULUS? ===")
show("L nonzero", f"{nzL:,} = {nzL/max(1,frames):.4f} per frame")
show("R nonzero", f"{nzR:,} = {nzR/max(1,frames):.4f} per frame")
show("aligned at schedule index", align)
show("bit-exact vs played noise",
     f"{match:,}/{checked:,} = {100.0*match/max(1,checked):.4f}%")

print("\n=== VERDICT ===")
if fb is None:
    print("  No independent clock reference: rate claims are VOID, not zero.")
elif abs(fb[0] - RATE) / RATE < 0.01:
    print(f"  Device consumes at {fb[0]:,} Hz = 384 kHz (feedback endpoint).")
    if align is not None and match == checked and checked > RATE:
        print("  The stimulus is present, contiguous and bit-exact over the "
              "window:")
        print("  bit-perfect certification IS possible on this image.")
    else:
        print(f"  But only {100.0*match/max(1,checked):.2f}% of compared frames "
              f"match and fill is {nzL/max(1,frames):.3f}/ch/frame:")
        print("  the tap does not govern most of the capture stream, so the "
              "comparison")
        print("  is mostly not our data. Bit-perfect is VOID on this image.")
else:
    print(f"  Device feedback says {fb[0]:,} Hz, not {RATE:,} -- the endpoint "
          f"itself")
    print("  is not running at the requested rate. Nothing downstream can be "
          "certified")
    print("  against 384 kHz until that is fixed.")
