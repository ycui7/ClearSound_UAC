#!/usr/bin/env python3
"""What rate is the I2S bus actually running, against ONE clock?

Why this exists: every measurement so far filled capture at 0.399 of frames,
including with the tap DISARMED. A constant that rate across tap-on and
tap-off is not a property of the tap -- it is a clock ratio. The suspicion is
that the USB endpoint advertises 384 kHz while the audio loop turns at a lower
LRCLK rate, so USB replays samplesIn instead of getting a fresh word: that
reproduces "stale value repeated" and "0.4 of frames fresh" simultaneously.

Everything here is timed by a single monotonic clock, and the SCPI reads happen
OUTSIDE the window -- csd_diag.py spawns a python process and opens rawmidi per
query, so a query interleaved with the loop silently inflates the window and
makes every rate wrong by a few tens of percent. That is what made the earlier
numbers inconsistent.

tx_frames counts 2 per LRCLK frame (one put per half-frame), so LRCLK Hz =
tx_delta/2/elapsed. DEBUG:I2S? increments once per HandleSampleClock call and
that is called twice per iteration, so it is reported raw and halved.
"""
import ctypes, struct, subprocess, threading, time

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
RATE = 384000
WINDOW = 5.0
DC = 0x123456


def scpi(cmd):
    r = subprocess.run([f"{APP}/.venv/bin/python", "csd_diag.py",
                        "--backend", "rawmidi", "--timeout", "15", cmd],
                       cwd=APP, capture_output=True, text=True, timeout=40)
    return (r.stdout or "").strip().replace("\r", "")


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
    got_rate = d.value
    if alsa.snd_pcm_hw_params(pcm, hp) < 0:
        return None
    per = ctypes.c_uint(0)
    alsa.snd_pcm_hw_params_get_period_size(hp, ctypes.byref(per), 0)
    alsa.snd_pcm_prepare(pcm)
    return pcm, got_rate, per.value


print("IDN:", scpi("*IDN?"))
print("tap:", scpi("DEBUG:LOOPback 1"), "|", scpi("DEBUG:LOOPback?"))
scpi("DEBUG:LOOPback:CLEAR")
scpi("DEBUG:I2S:CLEAR")

c = openp(1)
p = openp(0)
if not c or not p:
    raise SystemExit("open failed")
cap, cap_rate, cap_per = c
play, play_rate, play_per = p
print(f"negotiated: play {play_rate} Hz period {play_per} | "
      f"capture {cap_rate} Hz period {cap_per}")

N = 19200
flat = struct.pack(f"<{2 * N}I", *([DC << 8] * (2 * N)))
stop = threading.Event()
w = {"sent": 0, "recovers": 0}


def writer():
    while not stop.is_set():
        n = alsa.snd_pcm_writei(play, flat, N)
        if n < 0:
            w["recovers"] += 1
            alsa.snd_pcm_recover(play, n, 1)
            continue
        w["sent"] += n


alsa.snd_pcm_start(play)
alsa.snd_pcm_start(cap)
t = threading.Thread(target=writer, daemon=True)
t.start()
time.sleep(0.4)                             # let both rings reach steady state

buf = (ctypes.c_char * (N * 8))()
cap_frames = 0
cap_nz = 0
reads = 0
errs = {}
t0 = time.monotonic()
while True:
    e = time.monotonic() - t0
    if e >= WINDOW:
        break
    n = alsa.snd_pcm_readi(cap, buf, N)
    if n <= 0:
        alsa.snd_pcm_recover(cap, n if n < 0 else -1, 1)
        alsa.snd_pcm_start(cap)
        k = f"{n} {alsa.snd_strerror(n).decode()} st={alsa.snd_pcm_state(cap)}"
        errs[k] = errs.get(k, 0) + 1
        continue
    reads += 1
    f = [x >> 8 for x in struct.unpack(f"<{n * 2}I", bytes(buf[:n * 8]))]
    cap_frames += n
    cap_nz += sum(1 for x in f if x)
elapsed = time.monotonic() - t0
stop.set()
t.join(timeout=2)

# read counters AFTER the window so SCPI latency cannot inflate it
tx = scpi("DEBUG:LOOPback?")
i2s = scpi("DEBUG:I2S?")
print(f"\nwindow            : {elapsed:.4f} s")
print(f"USB playback sent : {w['sent']} frames -> {w['sent']/elapsed:,.0f} Hz "
      f"(recovers {w['recovers']})")
print(f"USB capture got   : {cap_frames} frames -> {cap_frames/elapsed:,.0f} Hz "
      f"({reads} reads, errs {errs})")
print(f"capture nonzero   : {cap_nz} -> {cap_nz/max(1,cap_frames):.4f}/frame")
print(f"tap tx_frames     : {tx!r} -> LRCLK {int(tx.split(',')[1])/elapsed:,.0f} Hz"
      f" (2 puts per LRCLK frame)")
try:
    fr = int(i2s.split(",")[0])
    bad = int(i2s.split(",")[1])
    print(f"DEBUG:I2S?        : frames={fr} bad={bad} ({bad/max(1,fr):.1%}) "
          f"-> {fr/elapsed:,.0f}/s raw, {fr/2/elapsed:,.0f}/s if 2 calls/frame")
except Exception:
    print(f"DEBUG:I2S?        : {i2s!r}")

print("\nVERDICT")
lrclk = int(tx.split(",")[1]) / 2 / elapsed
if abs(lrclk - RATE) / RATE < 0.01:
    print(f"  I2S LRCLK {lrclk:,.0f} Hz ~= {RATE}: bus is at rate. The 0.4 fill "
          f"is NOT a clock ratio -> investigate the substitution site.")
else:
    print(f"  I2S LRCLK {lrclk:,.0f} Hz vs USB {RATE} Hz -> MISMATCH "
          f"({lrclk/RATE:.3f}x). USB is served {RATE/lrclk:.2f}x faster than the "
          f"bus produces, so capture REPLAYS the previous samplesIn. That, not "
          f"the tap, is the 0.399 fill rate, and no bit-perfect claim is valid "
          f"until the bus runs at the advertised rate.")
