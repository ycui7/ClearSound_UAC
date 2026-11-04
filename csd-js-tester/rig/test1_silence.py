#!/usr/bin/env python3
"""TEST 1 (user spec): output dead silence with the I2S loopback tap ON ->
host capture must be ALL ZEROS, proving ADC values are not being sent to host.

A bare all-zero result would pass vacuously if the tap were stuck at zero, so
the run has three phases and the zero result is only credited because the
control phases bracket it:

  P1 tap ON,  play DC 0x123456 -> capture must show the played word.
     (Positive control: the substitution path is alive and overrides ADC.)
  P2 tap ON,  play silence     -> capture must be ALL ZEROS. (The test.)
  P3 tap OFF, play DC          -> capture shows the REAL ADC. Records what the
     ADC actually contributes so the P2 zeros cannot be re-interpreted later.

Per-channel fill is reported separately (an aggregate count hides one dead
channel). Continuous reader thread keeps both rings live -- starting capture
and then blocking on playback overruns the 20 ms ring and fakes a dead tap."""
import ctypes, struct, subprocess, sys, threading, time

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
DC = 0x123456
RATE = 384000


def scpi(cmd):
    r = subprocess.run([f"{APP}/.venv/bin/python", "csd_diag.py",
                        "--backend", "rawmidi", "--timeout", "15", cmd],
                       cwd=APP, capture_output=True, text=True, timeout=40)
    return (r.stdout or "").strip().replace("\r", "")


alsa = ctypes.CDLL("libasound.so.2")
for f, r, a in (("snd_pcm_format_value", ctypes.c_int, [ctypes.c_char_p]),
                ("snd_pcm_format_name", ctypes.c_char_p, [ctypes.c_int]),
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
            print(f"rejected {fn.__name__}")
            return None
    d = ctypes.c_uint(RATE)
    alsa.snd_pcm_hw_params_set_rate_near(pcm, hp, ctypes.byref(d), 0)
    if d.value != RATE:
        sys.exit(f"negotiated rate {d.value} != {RATE}")
    if alsa.snd_pcm_hw_params(pcm, hp) < 0:
        return None
    alsa.snd_pcm_prepare(pcm)
    return pcm


stop = threading.Event()
play_word = [DC << 8]           # mutable: phase sets it
N = 19200
wstat = {"sent": 0, "recovers": 0}


def writer(play):
    while not stop.is_set():
        v = play_word[0]
        flat = struct.pack(f"<{2 * N}I", *([v] * (2 * N)))
        w = alsa.snd_pcm_writei(play, flat, N)
        if w < 0:
            wstat["recovers"] += 1
            alsa.snd_pcm_recover(play, w, 1)
            continue
        wstat["sent"] += w


def phase(name, cap, play, word, tap, secs):
    play_word[0] = word
    stats = {"frames": 0, "nzL": 0, "nzR": 0, "valsL": set(), "valsR": set(),
             "reads": 0, "errs": {}}
    buf = (ctypes.c_char * (N * 8))()
    t0 = time.time()
    while time.time() - t0 < secs:
        got = alsa.snd_pcm_readi(cap, buf, N)
        if got <= 0:
            alsa.snd_pcm_recover(cap, got if got < 0 else -1, 1)
            alsa.snd_pcm_start(cap)
            e = (f"{got} {alsa.snd_strerror(got).decode()} "
                 f"state={alsa.snd_pcm_state(cap)}")
            stats["errs"][e] = stats["errs"].get(e, 0) + 1
            continue
        stats["reads"] += 1
        fields = [w >> 8 for w in
                  struct.unpack(f"<{got * 2}I", bytes(buf[:got * 8]))]
        Ls, Rs = fields[0::2], fields[1::2]
        stats["frames"] += got
        stats["nzL"] += sum(1 for x in Ls if x)
        stats["nzR"] += sum(1 for x in Rs if x)
        stats["valsL"] |= {x for x in Ls if x}
        stats["valsR"] |= {x for x in Rs if x}
    f = stats["frames"]
    print(f"[{name}] tap={tap} played=0x{word >> 8:06X} frames={f} "
          f"reads={stats['reads']}")
    print(f"    L: nonzero={stats['nzL']} ({stats['nzL']/max(1,f):.3f}/frame) "
          f"distinct={len(stats['valsL'])} "
          f"sample={[hex(v) for v in list(stats['valsL'])[:3]]}")
    print(f"    R: nonzero={stats['nzR']} ({stats['nzR']/max(1,f):.3f}/frame) "
          f"distinct={len(stats['valsR'])} "
          f"sample={[hex(v) for v in list(stats['valsR'])[:3]]}")
    if stats["errs"]:
        print(f"    read errors: {stats['errs']}")
    return stats, f


print("IDN:", scpi("*IDN?"))
print("arm tap :", scpi("DEBUG:LOOPback 1"), "| readback:",
      scpi("DEBUG:LOOPback?"))
cap, play = openp(1), openp(0)
if not cap or not play:
    sys.exit(f"open failed cap={bool(cap)} play={bool(play)}")
alsa.snd_pcm_start(play)
alsa.snd_pcm_start(cap)
t = threading.Thread(target=writer, args=(play,), daemon=True)
t.start()
time.sleep(0.3)

rc = 0
# P1 positive control: tap ON + DC -> capture shows the played word
s1, _ = phase("P1 control tap-ON + DC", cap, play, DC << 8, 1, 2.0)
ok1 = (s1["nzL"] > 0 and s1["nzR"] > 0
       and s1["valsL"] == {DC} and s1["valsR"] == {DC})
print(f"    P1 {'PASS' if ok1 else 'FAIL'}: tap substitution alive, "
      f"values exactly as played")

# P2 the test: tap ON + dead silence -> capture must be ALL ZEROS
s2, f2 = phase("P2 TEST tap-ON + SILENCE", cap, play, 0, 1, 2.0)
ok2 = s2["nzL"] == 0 and s2["nzR"] == 0 and not s2["errs"]
print(f"    P2 {'PASS' if ok2 else 'FAIL'}: {'capture all zero' if ok2 else 'NONZERO LEAK: L=%d R=%d' % (s2['nzL'], s2['nzR'])}")
if s2["valsL"] or s2["valsR"]:
    print(f"    leaked values L={list(s2['valsL'])[:6]} R={list(s2['valsR'])[:6]}")

# P3 reference: tap OFF + DC -> what the REAL ADC sends (no tap substitution)
print("disarm tap:", scpi("DEBUG:LOOPback 0"), "| readback:",
      scpi("DEBUG:LOOPback?"))
s3, _ = phase("P3 ref tap-OFF + DC", cap, play, DC << 8, 0, 2.0)
print(f"    P3: real ADC contributed nonzero {s3['nzL'] + s3['nzR']} fields "
      f"(records what the ADC would leak if it were reaching the host)")

print("re-arm    :", scpi("DEBUG:LOOPback 1"), "| readback:",
      scpi("DEBUG:LOOPback?"))
stop.set()
t.join(timeout=2)
print("played:", wstat)
if not (ok1 and ok2):
    sys.exit("TEST1:FAIL")
print("TEST1:PASS  (silence in -> all-zero out with tap ON; substitution "
      "proven alive by P1; ADC reference recorded by P3)")
