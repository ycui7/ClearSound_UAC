#!/usr/bin/env python3
"""TEST 1 (user spec): with the I2S loopback tap ON, dead-silence output must
read back ALL ZEROS on the ADC stream.

WHAT THIS CAN AND CANNOT PROVE -- stated up front, because the first version of
this file overclaimed.
  It CAN prove: the value presented to the I2S transmit path is what reaches the
    host capture stream, exactly, including zero. Combined with the noise control
    below, that means the capture stream is the tapped TX data.
  It CANNOT prove on its own that the ADC is never sent to the host: with no
    analog drive on the codec's ADC input, the ADC reads ~0 anyway, so an
    all-zero capture would look identical whether or not ADC data were reaching
    the host. That is why the DC control in the first version was doubly useless
    -- a constant cannot show ordering, and silence cannot show an ADC leak.
  What would close it: drive the ADC input with a real analog signal and show
    capture stays zero while the tap is armed. Recorded in TODO as needing
    bench hardware.

So this run establishes the substitution behaviour as precisely as the bench
allows, and the noise control is what carries the weight:
  P1 tap ON  + stereo noise out -> capture equals the played noise, frame for
      frame, at the right ORDER. A constant could not show this: replayed,
      dropped, duplicated or reordered frames are invisible in a DC stream.
  P2 tap ON  + dead silence     -> capture all zero            (the user's test)
  P3 tap OFF + stereo noise out -> what the real ADC contributes: the baseline
      that would show up if the ADC path leaked to the host.

Both phases pace the writer to the device (feedback endpoint) and purge both
rings at the boundary. Unpaced, the writer ran at 571 k frames/s into a 2000 ms
ring and P2 measured P1's backlog seconds later -- which produced a false FAIL.
"""
import ctypes, os, struct, subprocess, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import csd_signal as sig

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
CARD = os.environ.get("CSD_CARD", "3")
RATE = sig.RATE
PHASE_SECS, SETTLE = 3.0, 0.40
N = 4800                                   # 12.5 ms: fine pacing


def scpi(cmd):
    r = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                        "--timeout", "15", cmd], cwd=APP,
                       capture_output=True, text=True, timeout=40)
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


def resolve(want):
    for v in range(64):
        nm = alsa.snd_pcm_format_name(v)
        if nm is None:
            continue
        nb = nm if isinstance(nm, bytes) else nm.encode()
        if nb.decode().upper() == want.upper():
            assert alsa.snd_pcm_format_value(nb) == v, "format resolver lies"
            return v
    sys.exit(f"no ALSA format {want}")


FMT = resolve("S32_LE")


def openp(stream):
    pcm = ctypes.c_void_p()
    if alsa.snd_pcm_open(ctypes.byref(pcm), f"hw:{CARD},0".encode(),
                         stream, 0) < 0:
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
    b = ctypes.c_uint(0)
    alsa.snd_pcm_hw_params_get_buffer_size(hp, ctypes.byref(b))
    alsa.snd_pcm_prepare(pcm)
    return pcm, b.value


print("IDN:", scpi("*IDN?"))
cap, cap_buf = openp(1)
play, play_buf = openp(0)
if not cap or not play:
    sys.exit("open failed")
print(f"rings {play_buf}/{cap_buf} frames ({play_buf/RATE*1000:.0f} ms)")
alsa.snd_pcm_start(play)
alsa.snd_pcm_start(cap)

stop = threading.Event()
# The player emits the schedule from an absolute frame counter; `mode` chooses
# noise or silence for the payload. Absolute indexing is what makes ordering
# testable at all.
mode = ["noise"]
wst = {"sent": 0, "rec": 0, "paced": 0, "n": 0}


def writer():
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
        idx = np.arange(wst["n"], wst["n"] + n, dtype=np.uint64)
        if mode[0] == "noise":
            L, R = sig.noise24_vec(idx, ch=0), sig.noise24_vec(idx, ch=1)
        else:
            L = sig.silence24_vec(idx)
            R = sig.silence24_vec(idx)
        inter = np.empty((n, 2), dtype="<u4")
        inter[:, 0] = L.astype(np.uint32) << np.uint32(8)
        inter[:, 1] = R.astype(np.uint32) << np.uint32(8)
        flat = inter.tobytes()
        w = alsa.snd_pcm_writei(play, flat, n)
        if w < 0:
            wst["rec"] += 1
            alsa.snd_pcm_recover(play, w, 1)
            continue
        wst["sent"] += w
        wst["n"] += w


import numpy as np
tw = threading.Thread(target=writer, daemon=True)
tw.start()


def purge():
    """No backlog may survive a phase boundary."""
    wst["n"] = 0
    for pcm in (play, cap):
        alsa.snd_pcm_drop(pcm)
        alsa.snd_pcm_prepare(pcm)
        alsa.snd_pcm_start(pcm)


def phase(name, want_noise, tap, secs):
    mode[0] = "noise" if want_noise else "silence"
    purge()
    scpi("DEBUG:LOOPback 1" if tap else "DEBUG:LOOPback 0")
    time.sleep(SETTLE)
    st = {"frames": 0, "nzL": 0, "nzR": 0, "match": 0, "checked": 0,
          "first_bad": None, "err": 0}
    buf = (ctypes.c_char * (N * 8))()
    n_read = 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        got = alsa.snd_pcm_readi(cap, buf, N)
        if got <= 0:
            alsa.snd_pcm_recover(cap, got if got < 0 else -1, 1)
            alsa.snd_pcm_start(cap)
            st["err"] += 1
            continue
        f = np.frombuffer(bytes(buf[:got * 8]), dtype="<u4")
        Lw = (f[0::2] >> np.uint32(8)).astype(np.int64)
        Rw = (f[1::2] >> np.uint32(8)).astype(np.int64)
        st["frames"] += got
        st["nzL"] += int((Lw != 0).sum())
        st["nzR"] += int((Rw != 0).sum())
        # Compare against the SAME absolute-index schedule the player used. The
        # reader counts frames from the same origin, so index alignment holds
        # only after the purge; a mismatch here is a real ordering defect.
        idx = np.arange(n_read, n_read + got, dtype=np.uint64)
        n_read += got
        if want_noise:
            eL = sig.noise24_vec(idx, ch=0).astype(np.int64)
            eR = sig.noise24_vec(idx, ch=1).astype(np.int64)
            ok = (Lw == eL) & (Rw == eR)
            st["checked"] += got
            st["match"] += int(ok.sum())
            if st["first_bad"] is None and not ok.all():
                k = int(np.nonzero(~ok)[0][0])
                st["first_bad"] = (k, int(Lw[k]), int(Rw[k]),
                                   int(eL[k]), int(eR[k]))
    fr = max(1, st["frames"])
    print(f"[{name}] tap={tap} payload={'noise' if want_noise else 'silence'} "
          f"frames={st['frames']} readerrs={st['err']}")
    print(f"    L nonzero {st['nzL']} ({st['nzL']/fr:.3f}/frame)   "
          f"R nonzero {st['nzR']} ({st['nzR']/fr:.3f}/frame)")
    if want_noise:
        pct = 100.0 * st["match"] / max(1, st["checked"])
        print(f"    bit-exact vs played noise: {st['match']}/{st['checked']} "
              f"= {pct:.4f}%")
        if st["first_bad"]:
            k, gl, gr, el, er = st["first_bad"]
            print(f"    first mismatch at {k}: got 0x{gl:06X},0x{gr:06X} "
                  f"want 0x{el:06X},0x{er:06X}")
    return st


ok1 = ok2 = False
s1 = phase("P1 control: tap-ON + NOISE out", True, 1, PHASE_SECS)
ok1 = (s1["checked"] > 0 and s1["match"] == s1["checked"])
print(f"    -> {'PASS' if ok1 else 'FAIL'}: capture reproduces the played "
      f"noise exactly, frame for frame")

s2 = phase("P2 TEST: tap-ON + SILENCE out", False, 1, PHASE_SECS)
ok2 = s2["nzL"] == 0 and s2["nzR"] == 0
print(f"    -> {'PASS' if ok2 else 'FAIL'}: "
      + ("capture all zero" if ok2
         else f"NONZERO L={s2['nzL']} R={s2['nzR']}"))

s3 = phase("P3 baseline: tap-OFF + NOISE out", True, 0, PHASE_SECS)
print(f"    -> with the tap disarmed the capture is the real ADC: nonzero "
      f"{s3['nzL'] + s3['nzR']} of {2*max(1,s3['frames'])} fields")
print("    NOTE: with no analog drive on the codec ADC input this baseline is")
print("    near-zero by itself, so it does NOT by itself exclude an ADC leak --")
print("    see the module docstring. Closing that needs an analog signal on the")
print("    ADC input while the DAC outputs silence.")

scpi("DEBUG:LOOPback 1")
stop.set()
tw.join(timeout=2)
print(f"paced writer: sent={wst['sent']} recovers={wst['rec']} "
      f"paces-waited={wst['paced']}")
print("TEST1:" + ("PASS" if (ok1 and ok2) else "FAIL"))
sys.exit(0 if (ok1 and ok2) else 1)
