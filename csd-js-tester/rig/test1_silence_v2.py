#!/usr/bin/env python3
"""TEST 1 (user spec): dead-silence output with the I2S loopback tap ON must
read back ALL ZEROS on the ADC stream -- proving ADC values are not reaching
the host.

Why this version exists. The first version FAILED with the previous phase's DC
word still in the capture, and that was the rig's fault, not the device's:
playback is USB-AUDIO ASYNC with a feedback endpoint, and I let the writer run
ahead at 571 k frames/s against a 384 k device -- ~1.5x real time -- so seconds
of the previous stimulus sat in the pipeline and was still being played when I
switched to silence. Two rules fix it, both enforced below:

  1. PACE the writer to the device (avail_update). Never hand it more than it
     has asked for, so there is no backlog to leak across a phase boundary.
  2. PURGE both rings at every phase boundary (drop + prepare) and discard a
     settle window, so a phase measures only audio produced in that phase.

Three phases, and the all-zero result is only credited because a positive
control brackets it -- an all-zero capture would pass vacuously if the tap were
stuck at zero:

  P1 tap ON,  play DC 0x123456 -> capture must show exactly that word.
  P2 tap ON,  play silence     -> capture must be ALL ZEROS          (the test)
  P3 tap OFF, play DC          -> what the real ADC contributes       (reference)
"""
import ctypes, struct, subprocess, sys, threading, time

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
DC, RATE = 0x123456, 384000
PHASE_SECS, SETTLE = 3.0, 0.35
N = 4800            # 12.5 ms chunks: fine-grained pacing


def scpi(cmd):
    r = subprocess.run([f"{APP}/.venv/bin/python", "csd_diag.py",
                        "--backend", "rawmidi", "--timeout", "15", cmd],
                       cwd=APP, capture_output=True, text=True, timeout=40)
    return (r.stdout or "").strip().replace("\r", "")


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
    if d.value != RATE:
        print(f"  note: negotiated {d.value} not {RATE}")
    if alsa.snd_pcm_hw_params(pcm, hp) < 0:
        return None
    bufsz = ctypes.c_uint(0)
    alsa.snd_pcm_hw_params_get_buffer_size(hp, ctypes.byref(bufsz))
    alsa.snd_pcm_prepare(pcm)
    return pcm, bufsz.value


print("IDN:", scpi("*IDN?"))
c, p = openp(1), openp(0)
if not c or not p:
    sys.exit("open failed")
cap, cap_buf = c
play, play_buf = p
print(f"rings: playback buffer {play_buf} frames ({play_buf/RATE*1000:.1f} ms), "
      f"capture {cap_buf} frames ({cap_buf/RATE*1000:.1f} ms)")
alsa.snd_pcm_start(play)
alsa.snd_pcm_start(cap)

stop = threading.Event()
word = [DC << 8]
wst = {"sent": 0, "rec": 0, "paced": 0}


def writer():
    """PACED: only write what the device has actually asked for. Running ahead
    of the feedback endpoint is what made the last run measure stale audio."""
    while not stop.is_set():
        av = alsa.snd_pcm_avail_update(play)
        if av < 0:
            alsa.snd_pcm_recover(play, av, 1)
            alsa.snd_pcm_start(play)
            continue
        if av < N:
            wst["paced"] += 1
            time.sleep(0.001)
            continue
        n = min(av, N)
        flat = struct.pack(f"<{2 * n}I", *([word[0]] * (2 * n)))
        w = alsa.snd_pcm_writei(play, flat, n)
        if w < 0:
            wst["rec"] += 1
            alsa.snd_pcm_recover(play, w, 1)
            continue
        wst["sent"] += w


tw = threading.Thread(target=writer, daemon=True)
tw.start()


def purge():
    """Kill any backlog so a phase cannot measure audio produced earlier."""
    for pcm in (play, cap):
        alsa.snd_pcm_drop(pcm)
        alsa.snd_pcm_prepare(pcm)
        alsa.snd_pcm_start(pcm)


def phase(name, wv, tap, secs):
    word[0] = wv
    purge()
    scpi("DEBUG:LOOPback 1" if tap else "DEBUG:LOOPback 0")
    time.sleep(SETTLE)                     # discard pipeline latency
    st = {"frames": 0, "nzL": 0, "nzR": 0, "vL": set(), "vR": set(), "err": 0}
    buf = (ctypes.c_char * (N * 8))()
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        got = alsa.snd_pcm_readi(cap, buf, N)
        if got <= 0:
            alsa.snd_pcm_recover(cap, got if got < 0 else -1, 1)
            alsa.snd_pcm_start(cap)
            st["err"] += 1
            continue
        f = [x >> 8 for x in
             struct.unpack(f"<{got * 2}I", bytes(buf[:got * 8]))]
        Ls, Rs = f[0::2], f[1::2]
        st["frames"] += got
        st["nzL"] += sum(1 for x in Ls if x)
        st["nzR"] += sum(1 for x in Rs if x)
        st["vL"] |= {x for x in Ls if x}
        st["vR"] |= {x for x in Rs if x}
    fr = max(1, st["frames"])
    print(f"[{name}] tap={tap} played=0x{wv >> 8:06X} frames={st['frames']} "
          f"readerrs={st['err']}")
    for ch in ("L", "R"):
        nz, vs = st[f"nz{ch}"], st[f"v{ch}"]
        print(f"    {ch}: nonzero={nz} ({nz/fr:.3f}/frame) "
              f"distinct={len(vs)} sample={[hex(v) for v in list(vs)[:3]]}")
    return st


ok1 = ok2 = False
s1 = phase("P1 control tap-ON + DC", DC << 8, 1, PHASE_SECS)
ok1 = bool(s1["nzL"]) and bool(s1["nzR"]) and s1["vL"] == {DC} and s1["vR"] == {DC}
print(f"    -> {'PASS' if ok1 else 'FAIL'}: substitution alive, values exactly "
      f"as played")

s2 = phase("P2 TEST tap-ON + SILENCE", 0, 1, PHASE_SECS)
ok2 = s2["nzL"] == 0 and s2["nzR"] == 0
if ok2:
    print("    -> PASS: capture all zero -> ADC is NOT reaching the host")
else:
    print(f"    -> FAIL: LEAK L={s2['nzL']} R={s2['nzR']} "
          f"vals={list(s2['vL'])[:5]}")

s3 = phase("P3 reference tap-OFF + DC", DC << 8, 0, PHASE_SECS)
print(f"    -> real ADC contributed {s3['nzL'] + s3['nzR']} nonzero fields "
      f"(what the ADC would leak if it were reaching the host)")

scpi("DEBUG:LOOPback 1")
stop.set()
tw.join(timeout=2)
print(f"paced writer: sent={wst['sent']} recovers={wst['rec']} "
      f"paces-waited={wst['paced']}")
print("TEST1:" + ("PASS" if (ok1 and ok2) else "FAIL"))
sys.exit(0 if (ok1 and ok2) else 1)
