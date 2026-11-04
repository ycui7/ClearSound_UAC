#!/usr/bin/env python3
"""What did the USB stack actually negotiate? Read it from the kernel, don't infer.

Every rate conclusion so far has been arithmetic on our own counters, and the
counter semantics (puts per LRCLK frame, HandleSampleClock calls per frame) are
things I assumed. The kernel already knows the truth: it picked an alt-setting,
programmed the endpoint, and exposes it in /proc/asound/cardN/stream*. From the
isochronous endpoint's maxpacketsize and interval the nominal rate follows
directly, with no assumptions of ours in the chain.

Run while both streams are live -- the files only exist while open.
"""
import ctypes, glob, struct, subprocess, threading, time

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
RATE = 384000

alsa = ctypes.CDLL("libasound.so.2")
for f, r, a in (("snd_pcm_format_name", ctypes.c_char_p, [ctypes.c_int]),
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
    if alsa.snd_pcm_hw_params(pcm, hp) < 0:
        return None
    alsa.snd_pcm_prepare(pcm)
    return pcm


play, cap = openp(0), openp(1)
if not play or not cap:
    raise SystemExit("open failed")
N = 19200
flat = struct.pack(f"<{2 * N}I", *([0x123456 << 8] * (2 * N)))
stop = threading.Event()


def w():
    while not stop.is_set():
        n = alsa.snd_pcm_writei(play, flat, N)
        if n < 0:
            alsa.snd_pcm_recover(play, n, 1)
            continue


def r():
    buf = (ctypes.c_char * (N * 8))()
    while not stop.is_set():
        n = alsa.snd_pcm_readi(cap, buf, N)
        if n <= 0:
            alsa.snd_pcm_recover(cap, n if n < 0 else -1, 1)
            alsa.snd_pcm_start(cap)


alsa.snd_pcm_start(play)
alsa.snd_pcm_start(cap)
tw = threading.Thread(target=w, daemon=True)
tr = threading.Thread(target=r, daemon=True)
tw.start(); tr.start()
time.sleep(1.0)

print("=== /proc/asound/card3/stream* (kernel's view; exists only while open) ===")
for p in sorted(glob.glob("/proc/asound/card3/stream*")) + sorted(
        glob.glob("/proc/asound/card3/pcm0[pc]/sub0/*")):
    if p.endswith(("sw_params",)):
        continue
    try:
        txt = open(p).read().strip()
    except Exception as e:
        txt = f"<{e}>"
    if txt:
        print(f"--- {p}")
        print(txt)

stop.set()
tw.join(timeout=2); tr.join(timeout=2)
