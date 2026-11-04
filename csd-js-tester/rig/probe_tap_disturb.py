#!/usr/bin/env python3
"""Is the loopback tap disturbing the I2S loop whose timing it measures?

The tap adds 4 calls per LRCLK frame (put on each TX half, take on each RX
half) inside lib_xua's audio-critical loop, which at 384 kHz has a hard
per-frame budget. HandleSampleClock counts every LRCLK timestamp delta that is
not its expected 32 ticks, so `bad%` is a direct read of loop health.

Same image, same load, tap armed vs disarmed. put()/take() both return
immediately when disarmed, so this isolates the tap's WORK (volatile stores and
the substituted values) from the mere presence of the calls. If bad% is the
same armed and disarmed, the tap is not the disturbance and the loop was
already behind -- a property of the build, which the bit-perfect rig has to
report rather than hide.

Everything is timed by one monotonic clock and every SCPI read happens OUTSIDE
the measured window: csd_diag.py spawns a python process and opens rawmidi per
query, so a query interleaved with the loop inflates the window and makes every
rate wrong by tens of percent. That is what made earlier numbers disagree.
"""
import ctypes, struct, subprocess, sys, threading, time

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
RATE, WINDOW = 384000, 6.0
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
                ("snd_pcm_avail_update", ctypes.c_long, [ctypes.c_void_p]),
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
    sys.exit("open failed")
alsa.snd_pcm_start(play)
alsa.snd_pcm_start(cap)

stop = threading.Event()
w = {"sent": 0, "rec": 0, "paced": 0}


def writer():
    """PACED to the device (feedback endpoint) so no backlog forms."""
    while not stop.is_set():
        av = alsa.snd_pcm_avail_update(play)
        if av < 0:
            alsa.snd_pcm_recover(play, av, 1)
            alsa.snd_pcm_start(play)
            continue
        if av < 4800:
            w["paced"] += 1
            time.sleep(0.001)
            continue
        n = min(av, 4800)
        flat = struct.pack(f"<{2 * n}I", *([DC << 8] * (2 * n)))
        r = alsa.snd_pcm_writei(play, flat, n)
        if r < 0:
            w["rec"] += 1
            alsa.snd_pcm_recover(play, r, 1)
            continue
        w["sent"] += r


def run(label, tap):
    scpi("DEBUG:LOOPback 1" if tap else "DEBUG:LOOPback 0")
    scpi("DEBUG:LOOPback:CLEAR")
    scpi("DEBUG:I2S:CLEAR")
    for pcm in (play, cap):
        alsa.snd_pcm_drop(pcm)
        alsa.snd_pcm_prepare(pcm)
        alsa.snd_pcm_start(pcm)
    st = {"frames": 0, "nz": 0, "err": 0}
    buf = (ctypes.c_char * (4800 * 8))()
    t0 = time.monotonic()
    while time.monotonic() - t0 < WINDOW:
        n = alsa.snd_pcm_readi(cap, buf, 4800)
        if n <= 0:
            alsa.snd_pcm_recover(cap, n if n < 0 else -1, 1)
            alsa.snd_pcm_start(cap)
            st["err"] += 1
            continue
        f = [x >> 8 for x in struct.unpack(f"<{n*2}I", bytes(buf[:n*8]))]
        st["frames"] += n
        st["nz"] += sum(1 for x in f if x)
    el = time.monotonic() - t0                    # counters read AFTER window
    i2s = scpi("DEBUG:I2S?")
    tx = scpi("DEBUG:LOOPback?")
    fr, bad = (int(x) for x in i2s.split(",")[:2])
    puts = int(tx.split(",")[1])
    print(f"\n[{label}] tap={'ON' if tap else 'OFF'}   window {el:.3f} s")
    print(f"  capture        {st['frames']:>9} frames {st['frames']/el:>10,.0f} /s"
          f"   fresh-value fill {st['nz']/max(1,st['frames'])/2:.4f}/ch/frame")
    print(f"  tap puts       {puts:>9}          {puts/el:>10,.0f} /s")
    print(f"  I2S clock calls{fr:>9}          {fr/el:>10,.0f} /s")
    print(f"  I2S BAD delta  {bad:>9}          = {bad/max(1,fr):6.2%}"
          f"   <-- loop health")
    return bad / max(1, fr), fr, puts


tw = threading.Thread(target=writer, daemon=True)
tw.start()
time.sleep(0.5)
b_on, fr_on, p_on = run("A tap armed", True)
b_off, fr_off, p_off = run("B tap disarmed", False)
stop.set()
tw.join(timeout=2)
print(f"\npaced writer: {w}")
print("\nVERDICT")
d = b_on - b_off
if abs(d) < 0.02:
    print(f"  bad% tap-ON {b_on:.2%} vs tap-OFF {b_off:.2%} (delta {d:+.2%}):")
    print("  the tap is NOT the disturbance. The loop was already missing this")
    print("  many I2S deadlines in this build -- a property of the firmware at")
    print("  384k that the bit-perfect rig must report, not hide.")
else:
    print(f"  bad% tap-ON {b_on:.2%} vs tap-OFF {b_off:.2%} (delta {d:+.2%}):")
    print("  THE TAP IS THE DISTURBANCE. Its 4 calls/frame break the I2S")
    print("  budget, so no bit-perfect result from this instrument is valid")
    print("  until the tap is made cheaper or moved off the critical path.")
