#!/usr/bin/env python3
"""TEST 1 (user spec): with the I2S loopback tap ON, dead-silence output must
read back ALL ZEROS on the ADC stream.

WHAT THIS PROVES, AND WHAT IT DOES NOT -- stated up front because the first
version of this file overclaimed on both counts.

  PROVES: the word presented to the I2S transmit path is what reaches the host
    capture stream, bit for bit and IN ORDER, including the all-zero case. The
    noise control is what carries the weight: a constant could not show order,
    because in a DC stream a replayed, dropped, duplicated or reordered frame is
    indistinguishable from a fresh one.

  DOES NOT PROVE, on its own, that ADC data can never reach the host: with no
    analog drive on the codec's ADC input the ADC reads ~0 anyway, so an
    all-zero capture looks identical whether or not the ADC path is wired to the
    host. Closing that needs a real analog signal on the ADC input while the DAC
    outputs silence -- recorded as a bench-hardware TODO, not claimed here.

Alignment is found FROM THE SIGNAL, not assumed: the first captured frame is
located by content inside the schedule, which yields the out->in offset. Comparing
at any other offset would report the pipeline's own delay as thousands of defects.

Both phases pace the writer to the device (this endpoint is ASYNC with a feedback
EP) and purge both rings at the boundary. Unpaced, the writer measured 571 k
frames/s into a 2000 ms ring, so a phase boundary measured audio produced seconds
earlier -- that is what produced an earlier false FAIL.
"""
import ctypes, os, struct, subprocess, sys, threading, time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import csd_signal as sig

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
CARD = os.environ.get("CSD_CARD", "3")
RATE = sig.RATE
PHASE_SECS, SETTLE = 3.0, 0.40
N = 4800                                       # 12.5 ms chunks
SEARCH = 1 << 21                               # frames: covers the 2 s ring


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
    """Ask ALSA for the name; never label an enum from memory (an earlier
    hand-typed dict called enum 10 'S24_4LE' and nearly retargeted the rig to a
    format that does not exist here)."""
    for v in range(64):
        nm = alsa.snd_pcm_format_name(v)
        if nm is None:
            continue
        nb = nm if isinstance(nm, bytes) else nm.encode()
        if nb.decode().upper() == want.upper():
            if alsa.snd_pcm_format_value(nb) != v:
                sys.exit(f"format resolver disagrees for {want}")
            return v
    sys.exit(f"no ALSA format {want}")


FMT = resolve("S32_LE")


def openp(stream):
    pcm = ctypes.c_void_p()
    if alsa.snd_pcm_open(ctypes.byref(pcm), f"hw:{CARD},0".encode(),
                         stream, 0) < 0:
        sys.exit(f"cannot open stream {stream}")
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
    b = ctypes.c_uint(0)
    alsa.snd_pcm_hw_params_get_buffer_size(hp, ctypes.byref(b))
    alsa.snd_pcm_prepare(pcm)
    return pcm, b.value


print("IDN:", scpi("*IDN?"))
cap, cap_buf = openp(1)
play, play_buf = openp(0)
print(f"rings {play_buf}/{cap_buf} frames ({play_buf/RATE*1000:.0f} ms)")
alsa.snd_pcm_start(play)
alsa.snd_pcm_start(cap)

stop = threading.Event()
mode = ["noise"]
wst = {"sent": 0, "rec": 0, "paced": 0, "n": 0, "t_write0": None}


def writer():
    """Emits the absolute-index schedule. Absolute indexing is what makes
    ordering testable: R = f(n), so any frame's expected value is computable
    from its index alone."""
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
            L = sig.noise24_vec(idx, ch=0)
            R = sig.noise24_vec(idx, ch=1)
        else:
            L = sig.silence24_vec(idx)
            R = sig.silence24_vec(idx)
        inter = np.empty((n, 2), dtype="<u4")
        inter[:, 0] = L.astype(np.uint32) << np.uint32(8)
        inter[:, 1] = R.astype(np.uint32) << np.uint32(8)
        if wst["t_write0"] is None:
            wst["t_write0"] = time.monotonic()
        w = alsa.snd_pcm_writei(play, inter.tobytes(), n)
        if w < 0:
            wst["rec"] += 1
            alsa.snd_pcm_recover(play, w, 1)
            continue
        wst["sent"] += w
        wst["n"] += w


tw = threading.Thread(target=writer, daemon=True)
tw.start()


def purge():
    """A surviving backlog makes a phase measure audio produced seconds ago."""
    wst["n"] = 0
    wst["t_write0"] = None
    for pcm in (play, cap):
        alsa.snd_pcm_drop(pcm)
        alsa.snd_pcm_prepare(pcm)
        alsa.snd_pcm_start(pcm)


def find_offset(Lw, Rw):
    """Locate the first captured pair inside the schedule by CONTENT.

    This is the synthetic-signal timing alignment the test needs: the offset IS
    the out->in delay in frames, and comparing at any other offset would report
    the pipeline's own latency as thousands of defects. Returns (offset,
    seconds_from_first_write) or (None, None)."""
    if len(Lw) == 0:
        return None, None
    want_l, want_r = int(Lw[0]), int(Rw[0])
    if want_l == 0 and want_r == 0:
        return None, None                    # silence: nothing to align on
    for lo in range(0, SEARCH, 65536):
        idx = np.arange(lo, lo + 65536, dtype=np.uint64)
        eL = sig.noise24_vec(idx, ch=0).astype(np.int64)
        eR = sig.noise24_vec(idx, ch=1).astype(np.int64)
        hit = np.nonzero((eL == want_l) & (eR == want_r))[0]
        if len(hit):
            return lo + int(hit[0]), None
    return None, None


def phase(name, want_noise, tap, secs):
    mode[0] = "noise" if want_noise else "silence"
    purge()
    scpi("DEBUG:LOOPback 1" if tap else "DEBUG:LOOPback 0")
    time.sleep(SETTLE)
    st = {"frames": 0, "nzL": 0, "nzR": 0, "match": 0, "checked": 0,
          "off": None, "first_bad": None, "err": 0}
    buf = (ctypes.c_char * (N * 8))()
    t0 = time.monotonic()
    t_first_read = None
    while time.monotonic() - t0 < secs:
        got = alsa.snd_pcm_readi(cap, buf, N)
        if got <= 0:
            alsa.snd_pcm_recover(cap, got if got < 0 else -1, 1)
            alsa.snd_pcm_start(cap)
            st["err"] += 1
            continue
        if t_first_read is None:
            t_first_read = time.monotonic()
        f = np.frombuffer(bytes(buf[:got * 8]), dtype="<u4")
        Lw = (f[0::2] >> np.uint32(8)).astype(np.int64)
        Rw = (f[1::2] >> np.uint32(8)).astype(np.int64)
        st["frames"] += got
        st["nzL"] += int((Lw != 0).sum())
        st["nzR"] += int((Rw != 0).sum())
        if not want_noise:
            continue
        if st["off"] is None:
            off, _ = find_offset(Lw, Rw)
            if off is None:
                continue                     # not aligned yet
            st["off"] = off
            st["base"] = 0                   # reader position at alignment
            st["read_at_align"] = st["frames"] - got
        if st["off"] is None:
            continue
        # compare this chunk at the discovered offset
        base = st["read_at_align"] + (st["off"])
        k0 = st["frames"] - got - st["read_at_align"]
        idx = np.arange(base + k0, base + k0 + got, dtype=np.uint64)
        eL = sig.noise24_vec(idx, ch=0).astype(np.int64)
        eR = sig.noise24_vec(idx, ch=1).astype(np.int64)
        ok = (Lw == eL) & (Rw == eR)
        st["checked"] += got
        st["match"] += int(ok.sum())
        if st["first_bad"] is None and not bool(ok.all()):
            k = int(np.nonzero(~ok)[0][0])
            st["first_bad"] = (k, int(Lw[k]), int(Rw[k]), int(eL[k]),
                               int(eR[k]))
    fr = max(1, st["frames"])
    print(f"[{name}] tap={tap} payload={'noise' if want_noise else 'silence'} "
          f"frames={st['frames']} readerrs={st['err']}")
    print(f"    nonzero L {st['nzL']} ({st['nzL']/fr:.3f}/frame)  "
          f"R {st['nzR']} ({st['nzR']/fr:.3f}/frame)")
    if want_noise:
        if st["off"] is None:
            print("    ALIGNMENT NOT FOUND -- cannot claim any comparison")
        else:
            print(f"    aligned at schedule offset {st['off']} frames "
                  f"= {st['off']/RATE*1000:.2f} ms (synthetic-signal alignment)")
            pct = 100.0 * st["match"] / max(1, st["checked"])
            print(f"    bit-exact vs played noise: {st['match']}/{st['checked']} "
                  f"= {pct:.4f}%")
            if st["first_bad"]:
                k, gl, gr, el, er = st["first_bad"]
                print(f"    first mismatch at {k}: got 0x{gl:06X},0x{gr:06X} "
                      f"want 0x{el:06X},0x{er:06X}")
    st["lat_ms"] = ((t_first_read - wst["t_write0"]) * 1000.0
                    if (t_first_read and wst["t_write0"]) else None)
    return st


ok1 = ok2 = False
s1 = phase("P1 control: tap-ON + NOISE out", True, 1, PHASE_SECS)
ok1 = (s1["off"] is not None and s1["checked"] > 0
       and s1["match"] == s1["checked"])
print(f"    -> {'PASS' if ok1 else 'FAIL'}: capture reproduces the played noise "
      f"exactly, frame for frame, in order")

s2 = phase("P2 TEST: tap-ON + SILENCE out", False, 1, PHASE_SECS)
ok2 = s2["nzL"] == 0 and s2["nzR"] == 0
print(f"    -> {'PASS' if ok2 else 'FAIL'}: "
      + ("capture all zero" if ok2 else f"NONZERO L={s2['nzL']} R={s2['nzR']}"))

s3 = phase("P3 baseline: tap-OFF + NOISE out", True, 0, PHASE_SECS)
print(f"    -> tap disarmed: capture is the real ADC -- nonzero "
      f"{s3['nzL'] + s3['nzR']} of {2*max(1,s3['frames'])} fields")

scpi("DEBUG:LOOPback 1")
stop.set()
tw.join(timeout=2)
print(f"\npaced writer: sent={wst['sent']} recovers={wst['rec']} "
      f"paces-waited={wst['paced']}")
if s1.get("lat_ms") is not None:
    print(f"first noise read {s1['lat_ms']:.1f} ms after the first write "
          f"(includes our own ring: see the queue-bound decomposition in "
          f"bit_perfect_noise.py)")
print("\nSCOPE NOTE: with no analog drive on the codec ADC input, the ADC reads")
print("~0 regardless, so P2 alone cannot exclude an ADC->host path. It proves")
print("the tapped TX words are what reach the host. Excluding an ADC leak needs")
print("an analog signal on the ADC input while the DAC outputs silence.")
print("TEST1:" + ("PASS" if (ok1 and ok2) else "FAIL"))
sys.exit(0 if (ok1 and ok2) else 1)
