#!/usr/bin/env python3
"""Find the sustainable I2S sample-rate ceiling, measured by pitch.

Method (independent of every firmware counter, and of ALSA's rate bookkeeping):
play a 1 kHz sine at the negotiated rate R and read it back through the loopback
tap. The capture is interpreted at R, so

    f_measured = 1000 * R / f_device

gives f_device, the rate at which the device's I2S loop actually consumes
samples. f_device == R means the loop is locked; f_device < R means the loop is
CPU-bound and free-running below the negotiated rate, which makes any bit-perfect
claim at that rate void -- data arrives in the right order but at the wrong
instants, and the gaps are filled with the underflow word.

Pitch is the right instrument because it is a property of the samples themselves:
a miscounted counter cannot shift a tone, and neither can a host-side pacing bug
(the capture rate is reported alongside so a starved reader is visible).
"""
import ctypes, os, sys, threading, time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import csd_signal as sig

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
CARD = os.environ.get("CSD_CARD", "3")
RATES = [int(r) for r in os.environ.get("CSD_RATES", "48000,96000,176400,192000,384000").split(",")]
WINDOW = 3.0
N = 1024


def subprocess_run(argv, _tag):
    import subprocess
    try:
        p = subprocess.run(argv, cwd=APP, capture_output=True, text=True, timeout=40)
    except Exception as e:
        return f"ERR {e}"
    return (p.stdout or "") + (p.stderr or "")


def scpi(cmd):
    r = subprocess_run([PY, "csd_diag.py", "--backend", "rawmidi",
                        "--timeout", "15", cmd], " ".join(cmd))
    return (r or "").strip().replace("\r", "")


alsa = ctypes.CDLL("libasound.so.2")
for f_, r_, a_ in (("snd_pcm_format_name", ctypes.c_char_p, [ctypes.c_int]),
                   ("snd_pcm_writei", ctypes.c_long,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
                   ("snd_pcm_readi", ctypes.c_long,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]),
                   ("snd_pcm_avail_update", ctypes.c_long, [ctypes.c_void_p]),
                   ("snd_pcm_recover", ctypes.c_int,
                    [ctypes.c_void_p, ctypes.c_int, ctypes.c_int])):
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


def openp(stream, rate):
    pcm = ctypes.c_void_p()
    if alsa.snd_pcm_open(ctypes.byref(pcm), f"hw:{CARD},0".encode(), stream, 0) < 0:
        return None
    hp = ctypes.c_void_p()
    alsa.snd_pcm_hw_params_malloc(ctypes.byref(hp))
    alsa.snd_pcm_hw_params_any(pcm, hp)
    for fn, arg in ((alsa.snd_pcm_hw_params_set_access, 3),
                    (alsa.snd_pcm_hw_params_set_format, FMT),
                    (alsa.snd_pcm_hw_params_set_channels, 2)):
        if fn(pcm, hp, arg) < 0:
            alsa.snd_pcm_close(pcm)
            return None
    d = ctypes.c_uint(rate)
    alsa.snd_pcm_hw_params_set_rate_near(pcm, hp, ctypes.byref(d), None)
    if d.value != rate:
        alsa.snd_pcm_close(pcm)
        return None
    if alsa.snd_pcm_hw_params(pcm, hp) < 0:
        alsa.snd_pcm_close(pcm)
        return None
    alsa.snd_pcm_prepare(pcm)
    return pcm


print("IDN:", scpi("*IDN?"))
print("arm :", scpi("DEBUG:LOOPback 1"))
# Runtime A/B lever: the per-frame DSP (FD/FIR engine in UserBufferManagement)
# can be switched OFF through SCPI without rebuilding, so the SAME binary can be
# measured with and without that load on the audio core. That isolates which
# per-frame work owns the missed deadline -- a rebuild-based A/B is weaker here
# because PCM_DSP_EN=0 does not even compile (csd_mb_note_hwcfg's prototype sits
# behind the same gate).
# Runtime A/B lever: the generator is per-frame work too, and it can be switched
# off over SCPI without rebuilding -- same binary, one variable.
if os.environ.get("CSD_GEN_OFF") == "1":
    print("gen off:", scpi("GENerator:OFF"))
    print("gen?   :", scpi("GENerator:STATe?"))
PRESET = os.environ.get("CSD_PRESET")
if PRESET is not None:
    print(f"preset-> {PRESET}:", scpi(f"DIAG:DSP:PRESet {PRESET}"))
    print("preset? :", scpi("DIAG:DSP:PRESet?"))
print(f"{'rate':>8} {'cap Hz':>10} {'fill':>6} {'peak Hz':>10} "
      f"{'f_device':>10} {'ratio':>7}  verdict")
rows = []
for rate in RATES:
    scpi("DEBUG:LOOPback:CLEAR")
    play, cap = openp(0, rate), openp(1, rate)
    if not play or not cap:
        print(f"{rate:>8} {'--':>10} driver rejected this rate")
        rows.append((rate, None))
        continue
    stop = threading.Event()
    st = {"sent": 0}

    def writer():
        n = 0
        while not stop.is_set():
            av = alsa.snd_pcm_avail_update(play)
            if av < 0:
                alsa.snd_pcm_recover(play, av, 1)
                alsa.snd_pcm_start(play)
                continue
            if av < N:
                time.sleep(0.0005)
                continue
            k = min(av, N)
            idx = np.arange(n, n + k, dtype=np.uint64)
            inter = np.empty((k, 2), dtype="<u4")
            # tone24_vec divides by the MODULE-level RATE, so a leg at 48 kHz
            # must re-point it or the synthesised pitch is wrong by rate/384k
            # -- which is exactly what made every non-384k leg report a
            # "device rate" of 384 kHz.
            sig.RATE = rate
            inter[:, 0] = sig.tone24_vec(idx, freq=1000.0, ch=0).astype(np.uint32) << np.uint32(8)
            inter[:, 1] = sig.tone24_vec(idx, freq=1000.0, ch=1).astype(np.uint32) << np.uint32(8)
            r = alsa.snd_pcm_writei(play, inter.tobytes(), k)
            if r < 0:
                alsa.snd_pcm_recover(play, r, 1)
                alsa.snd_pcm_start(play)
                continue
            st["sent"] += k
            n += k

    alsa.snd_pcm_start(play)
    alsa.snd_pcm_start(cap)
    tw = threading.Thread(target=writer, daemon=True)
    tw.start()
    time.sleep(0.5)
    buf = (ctypes.c_char * (N * 8))()
    got = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < WINDOW:
        r = alsa.snd_pcm_readi(cap, buf, N)
        if r <= 0:
            alsa.snd_pcm_recover(cap, r if r < 0 else -1, 1)
            alsa.snd_pcm_start(cap)
            continue
        got.append(bytes(buf[:r * 8]))
    el = time.monotonic() - t0
    stop.set()
    tw.join(timeout=2)
    for p_ in (play, cap):
        alsa.snd_pcm_drop(p_)
        alsa.snd_pcm_close(p_)

    if not got:
        print(f"{rate:>8} {'0':>10} no capture")
        rows.append((rate, None))
        continue
    f = np.concatenate([np.frombuffer(c, dtype="<u4") for c in got])
    Lw = (f[0::2] >> np.uint32(8)).astype(np.float64)
    frames = len(f) // 2
    fill = float((Lw != 0).mean())
    if fill < 0.02:
        print(f"{rate:>8} {frames/el:>10,.0f} {fill:>6.3f} "
              f"{'--':>10} {'--':>10} {'--':>7}  no data (VOID)")
        rows.append((rate, None))
        continue
    x = Lw - Lw.mean()
    X = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    peak = int(np.argmax(X)) * rate / len(x)
    fdev = 1000.0 * rate / peak if peak else 0.0
    ratio = fdev / rate
    verdict = "LOCKED" if abs(ratio - 1.0) < 0.02 else (
        f"FREE-RUNNING at {ratio:.3f}x -> bit-perfect VOID")
    print(f"{rate:>8} {frames/el:>10,.0f} {fill:>6.3f} {peak:>10,.1f} "
          f"{fdev:>10,.0f} {ratio:>7.3f}  {verdict}")
    rows.append((rate, ratio))

ok = [r for r, v in rows if v and abs(v - 1.0) < 0.02]
bad = [r for r, v in rows if v and abs(v - 1.0) >= 0.02]
print()
if ok:
    print(f"locked up to: {max(ok):,} Hz")
if bad:
    print(f"free-running at: {', '.join(f'{b:,}' for b in sorted(bad))}")
    print("The 384 kHz bit-perfect gate cannot be certified until the loop locks.")
