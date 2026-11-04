#!/usr/bin/env python3
"""Is the loopback tap itself disturbing the audio loop it measures?

The tap adds two function calls per channel per frame (put on the TX side,
take on the RX side) INSIDE lib_xua's audio-critical I/O loop, which at
384 kHz has a hard per-frame budget. If the loop overruns, HandleSampleClock
sees the wrong BCLK delta and increments the `bad` counter -- so `bad%` is a
direct read of loop health, and comparing it between a tap build and a build
without the tap tells us whether the instrument is the disturbance.

This matters before any bit-perfect claim: a probe that changes the thing it
measures cannot certify it. Run identically against two images and compare.

Also captures the USB-side rates so the two views can be reconciled.
"""
import ctypes, struct, subprocess, sys, threading, time

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
RATE = 384000
WINDOW = 5.0
DC = 0x123456


def scpi(cmd):
    r = subprocess.run([f"{APP}/.venv/bin/python", "csd_diag.py",
                        "--backend", "rawmidi", "--timeout", "15", cmd],
                       cwd=APP, capture_output=True, text=True, timeout=40)
    return (r.stdout or "").strip().replace("\r", "")


print("IDN:", scpi("*IDN?"))

alsa = ctypes.CDLL("libasound.so.2")
for f, r, a in (("snd_pcm_format_name", ctypes.c_char_p, [ctypes.c_int]),
                ("snd_strerror", ctypes.c_char_p, [ctypes.c_int]),
                ("snd_pcm_state", ctypes.c_int, [ctypes.c_void_p]),
                ("snd_pcm_writei", ctypes.c_long,
                 [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
                ("snd_pcm_readi", ctypes.c_long,
                 [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong])):
    getattr(alsa, f).restype = r
    getattr(alsa, f).argtypes = a

FMT = next(v for v in range(64)
           if (alsa.snd_pcm_format_name(v) or b"").decode().upper() == "S32_LE")


def openp(stream):
    pcm = ctypes.c_void_p()
    if alsa.snd_pcm_open(ctypes.byref(pcm), b"hw:3,0", stream, 0) < 0:
        return None
    hp = ctypes.c_void_p()
    alsa.snd_pcm_hw_params_malloc(ctypes.byref(hp))
    alsa.snd_pcm_hw_params_any(pcm, hp)
    for fn, arg in ((alsa.snd_pcm_hw_params_set_access, 3),
                    (alsa.snd_pcm_hw_params_set_format, FMT),
                    (alsa.snd_pcm_hw_params_set_channels, 2)):
        if fn(pcm, hp, arg) < 0:
            return None
    d = ctypes.c_uint(RATE)
    alsa.snd_pcm_hw_params_set_rate_near(pcm, hp, ctypes.byref(d), 0)
    if d.value != RATE:
        print(f"  note: negotiated rate {d.value}, not {RATE}")
    if alsa.snd_pcm_hw_params(pcm, hp) < 0:
        return None
    alsa.snd_pcm_prepare(pcm)
    return pcm


scpi("DEBUG:I2S:CLEAR")

play = openp(0)
cap = openp(1)
if not play or not cap:
    sys.exit("open failed")

N = 19200
flat = struct.pack(f"<{2 * N}I", *([DC << 8] * (2 * N)))
stop = threading.Event()
w = {"sent": 0, "rec": 0}


def writer():
    while not stop.is_set():
        n = alsa.snd_pcm_writei(play, flat, N)
        if n < 0:
            w["rec"] += 1
            alsa.snd_pcm_recover(play, n, 1)
            continue
        w["sent"] += n


def reader(st):
    buf = (ctypes.c_char * (N * 8))()
    while not stop.is_set():
        n = alsa.snd_pcm_readi(cap, buf, N)
        if n <= 0:
            alsa.snd_pcm_recover(cap, n if n < 0 else -1, 1)
            alsa.snd_pcm_start(cap)
            st["err"] += 1
            continue
        f = [x >> 8 for x in struct.unpack(f"<{n * 2}I", bytes(buf[:n * 8]))]
        st["frames"] += n
        st["nz"] += sum(1 for x in f if x)


alsa.snd_pcm_start(play)
alsa.snd_pcm_start(cap)
st = {"frames": 0, "nz": 0, "err": 0}
tw = threading.Thread(target=writer, daemon=True)
tr = threading.Thread(target=reader, args=(st,), daemon=True)
t0 = time.monotonic()
tw.start(); tr.start()
time.sleep(WINDOW)
stop.set()
tw.join(timeout=3); tr.join(timeout=3)
el = time.monotonic() - t0

i2s = scpi("DEBUG:I2S?")
tx = scpi("DEBUG:LOOPback?")
try:
    fr, bad = int(i2s.split(",")[0]), int(i2s.split(",")[1])
except Exception:
    print(f"DEBUG:I2S? unreadable: {i2s!r}")
    sys.exit(1)

print(f"\nwindow        : {el:.3f} s")
print(f"playback sent : {w['sent']:>9} frames  {w['sent']/el:>10,.0f} Hz "
      f"(recovers {w['rec']})")
print(f"capture got   : {st['frames']:>9} frames  {st['frames']/el:>10,.0f} Hz "
      f"(errors {st['err']})")
if st["frames"]:
    print(f"capture nonzero: {st['nz']} -> {st['nz']/st['frames']:.4f}/frame")
print(f"LOOPback?     : {tx!r}")
print(f"\n*** I2S LOOP HEALTH (the number that matters) ***")
print(f"    HandleSampleClock calls : {fr:>9}  {fr/el:>10,.0f} /s")
print(f"    bad LRCLK delta         : {bad:>9}  = {bad/max(1,fr):6.2%}")
print(f"    (delta should be 32 BCLK ticks per LRCLK half-period; a high bad%")
print(f"     means the audio loop misses its per-frame budget.)")
