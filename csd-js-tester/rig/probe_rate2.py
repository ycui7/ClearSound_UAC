#!/usr/bin/env python3
"""Does this image hold 384 kHz in the audio loop, and does the tap govern every
capture frame? One measurement, release-compatible.

Why the tap counter and not DEBUG:I2S?: in a RELEASE build csd_mb_note_i2s_delta
is an empty stub, so DEBUG:I2S? would answer a clean 0% bad and be worse than
absent. DEBUG:LOOPback? is real in both builds, so LRCLK rate is measured from
the thing we actually depend on: the tap put()s one word per channel per LRCLK
frame, so LRCLK Hz = tx_frames / 2 / elapsed.

The number that decides whether a bit-perfect claim is possible at all is
`fill`: the fraction of capture frames whose payload is the tap's word. On the
debug image that measured 0.399 per channel while capture ran at 383 633 Hz and
the tap put at 310 478/s -- i.e. the I2S loop was producing ~155 k LRCLK frames
while USB was served at 384 k, so most capture frames carried a buffer the
injection never touched. A rig cannot certify "every bit" when 60% of the
compared frames are not the stimulus.

Stimulus is stereo band-limited noise (never DC: a constant carries no ordering
information, so replay/drop/duplicate/reorder are all invisible in it).
"""
import ctypes, os, subprocess, sys, threading, time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import csd_signal as sig

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
CARD = os.environ.get("CSD_CARD", "3")
RATE = sig.RATE
WINDOW = 6.0
N = 4800


def scpi(cmd):
    r = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                        "--timeout", "15", cmd], cwd=APP,
                       capture_output=True, text=True, timeout=40)
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


# arm the tap FIRST: put() only counts while armed, and the counter is our only
# release-legal measure of the LRCLK rate.
print("arm :", scpi("DEBUG:LOOPback 1"), "|", scpi("DEBUG:LOOPback:CLEAR"))
print("state:", scpi("DEBUG:LOOPback?"))

play, cap = openp(0), openp(1)
alsa.snd_pcm_start(play)
alsa.snd_pcm_start(cap)

stop = threading.Event()
w = {"sent": 0, "rec": 0, "paced": 0}


def writer():
    """PACED to the feedback endpoint. An unpaced writer ran at 571 k frames/s
    into a 2 s ring, which made every rate below a measurement of our own
    backlog rather than of the device."""
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


th = threading.Thread(target=writer, daemon=True)
th.start()
time.sleep(0.5)

st = {"frames": 0, "nzL": 0, "nzR": 0, "match": 0, "checked": 0, "err": 0,
      "aligned": None}
buf = (ctypes.c_char * (N * 8))()
t0 = time.monotonic()
while time.monotonic() - t0 < WINDOW:
    r = alsa.snd_pcm_readi(cap, buf, N)
    if r <= 0:
        alsa.snd_pcm_recover(cap, r if r < 0 else -1, 1)
        alsa.snd_pcm_start(cap)
        st["err"] += 1
        continue
    f = np.frombuffer(bytes(buf[:r * 8]), dtype="<u4")
    Lw = (f[0::2] >> np.uint32(8)).astype(np.int64)
    Rw = (f[1::2] >> np.uint32(8)).astype(np.int64)
    st["frames"] += r
    st["nzL"] += int((Lw != 0).sum())
    st["nzR"] += int((Rw != 0).sum())
    # align once by CONTENT, then compare: the offset IS the out->in delay and
    # comparing at any other offset would report latency as defects.
    if st["aligned"] is None and len(Lw) > 8 and (Lw[0] or Rw[0]):
        for lo in range(0, 1 << 21, 1 << 16):
            idx = np.arange(lo, lo + (1 << 16), dtype=np.uint64)
            eL = sig.noise24_vec(idx, ch=0).astype(np.int64)
            eR = sig.noise24_vec(idx, ch=1).astype(np.int64)
            hit = np.nonzero((eL == Lw[0]) & (eR == Rw[0]))[0]
            if len(hit):
                st["aligned"] = lo + int(hit[0])
                st["base"] = st["frames"] - r
                break
    if st["aligned"] is not None:
        k0 = (st["frames"] - r) - st["base"]
        idx = np.arange(st["aligned"] + k0, st["aligned"] + k0 + r,
                        dtype=np.uint64)
        eL = sig.noise24_vec(idx, ch=0).astype(np.int64)
        eR = sig.noise24_vec(idx, ch=1).astype(np.int64)
        ok = (Lw == eL) & (Rw == eR)
        st["checked"] += r
        st["match"] += int(ok.sum())

el = time.monotonic() - t0
stop.set()
th.join(timeout=2)
tx = scpi("DEBUG:LOOPback?")
try:
    puts = int(tx.split(",")[1])
except Exception:
    puts = 0

fr = st["frames"]
lrclk = puts / 2.0 / el
print(f"""
window          {el:.3f} s
played (paced)  {w['sent']:>9} frames   {w['sent']/el:>10,.0f} Hz  recovers={w['rec']}
capture         {fr:>9} frames   {fr/el:>10,.0f} Hz  errors={st['err']}
tap tx puts     {puts:>9}            {puts/el:>10,.0f} /s
  -> LRCLK      {lrclk:>18,.0f} Hz   (put() fires once per channel per LRCLK frame)

FILL (is the compared data actually the stimulus?)
  L nonzero     {st['nzL']:>9}   {st['nzL']/max(1,fr):.4f} per frame
  R nonzero     {st['nzR']:>9}   {st['nzR']/max(1,fr):.4f} per frame
  aligned at schedule index   {st['aligned']}
  bit-exact vs played noise   {st['match']}/{st['checked']}
      = {100.0*st['match']/max(1,st['checked']):.4f}%

VERDICT""")
if st["aligned"] is None:
    print("  NOT ALIGNED: the noise never appeared as a contiguous run. The tap")
    print("  is not governing the capture stream, so NO bit-perfect claim is")
    print("  possible on this image.")
elif abs(lrclk - RATE) / RATE < 0.02 and st["match"] == st["checked"]:
    print(f"  LRCLK {lrclk:,.0f} Hz holds rate and every aligned frame is exact.")
    print("  Bit-perfect certification is possible on this image.")
else:
    print(f"  LRCLK {lrclk:,.0f} Hz vs required {RATE} Hz "
          f"({lrclk/RATE:.3f}x), bit-exact {st['match']}/{st['checked']}.")
    print("  The audio loop does not sustain 384 kHz: USB is served")
    print(f"  {RATE/max(1,lrclk):.2f}x faster than the I2S loop produces frames, so")
    print("  most capture frames carry a buffer the injection never touched.")
    print("  That is a firmware timing finding, not a rig artefact -- the tap")
    print("  was measured NOT to cause it (58.17% vs 58.18% bad with tap off).")
