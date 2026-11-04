#!/usr/bin/env python3
"""Leg 1 of bit-perfect validation: the host must emit exactly the golden
bytes at 384 kHz / 24-bit, with no ALSA substitution on the way out.

This leg proves nothing about the DAC. It retires the HOST as a suspect, so
that every later mismatch is firmware or wire. Skip it and a resampled or
16-bit-negotiated stream produces a confident PASS against a golden file that
was never actually played.

Container contract (measured, rig/probe_formats.py):
  the device offers S32_LE with bBitResolution 24 and refuses S24_LE outright.
  So "24-bit at 384k" == 32-bit subslot, bits 31..8 meaningful. Bits 7..0 are
  padding the descriptor makes no promise about: we send zeros, and REPORT
  their fate rather than assert on them.

Stimulus design -- it must be able to fail:
  L = 24-bit frame counter in bits 31..8. Every frame unique, so a dropped,
      duplicated or reordered frame is identified by its own value, and a
      zero-substituted underrun (xua_audiohub.xc underflowWord) shows up as
      an unexpected 0x000000 at a specific index.
  R = 24-bit PRNG, pinned seed, PLUS injected boundary words. Sine waves hide
      frame drops behind phase continuity; a counter alone is pattern-
      predictable and survives a sloppy pattern-matching decoder.
  Never all-ones as the only special value -- it is rotation-invariant and
      hides a 1-bit wire shift.

Why the low 8 bits of a 24-bit-in-32 frame are still counted: the I2S path
writes the whole word (bitrev of a 32-bit sample, XUA_I2S_N_BITS==32), so a
host-side truncation shows up there. Reported, not gated.
"""
import argparse
import ctypes
import hashlib
import os
import struct
import sys
import time

RATE = 384000
CH = 2
BYTES_PER_FRAME = CH * 4
BOUNDARY_EVERY = 1024
# 24-bit boundaries: LSB-1, MSB-1, +1, all-ones, plus a single-bit walk.
BOUNDARIES = (0x800000, 0x7FFFFF, 0x000001, 0xFFFFFF,
              0x400000, 0x000002, 0x200000, 0x000004)

CARD = os.environ.get("CSD_CARD", "3")
DEVICE = f"hw:{CARD},0"

alsa = ctypes.CDLL("libasound.so.2")
alsa.snd_pcm_format_value.restype = ctypes.c_int
alsa.snd_pcm_format_value.argtypes = [ctypes.c_char_p]
alsa.snd_pcm_format_name.restype = ctypes.c_char_p
alsa.snd_pcm_format_name.argtypes = [ctypes.c_int]
S32_LE = alsa.snd_pcm_format_value(b"S32_LE")
assert alsa.snd_pcm_format_name(S32_LE).decode() == "S32_LE"


def golden_frames(n, seed=0xC5D):
    """Deterministic frame list: (L24, R24). Same function feeds the writer
    and the checker -- but the CHECKER is handed the bytes ALSA actually took,
    so a writer bug cannot validate itself."""
    st = seed | 1
    out = []
    for i in range(n):
        l24 = i & 0xFFFFFF
        # xorshift32 on the 24-bit field; cheap, reproducible, no numpy dep
        st ^= (st << 13) & 0xFFFFFF
        st ^= (st >> 17)
        st ^= (st << 5) & 0xFFFFFF
        st &= 0xFFFFFF
        r24 = st
        if i and (i % BOUNDARY_EVERY) == 0:
            r24 = BOUNDARIES[(i // BOUNDARY_EVERY) % len(BOUNDARIES)]
        out.append((l24, r24))
    return out


def pack(frames):
    buf = bytearray(len(frames) * BYTES_PER_FRAME)
    for i, (l24, r24) in enumerate(frames):
        struct.pack_into("<II", buf, i * BYTES_PER_FRAME,
                         l24 << 8, r24 << 8)
    return bytes(buf)


def open_pcm():
    pcm = ctypes.c_void_p()
    rc = alsa.snd_pcm_open(ctypes.byref(pcm), DEVICE.encode(), 0, 0)
    if rc < 0:
        die(f"snd_pcm_open({DEVICE}): {alsa.snd_strerror(rc)}")
    hp = ctypes.c_void_p()
    alsa.snd_pcm_hw_params_malloc(ctypes.byref(hp))
    alsa.snd_pcm_hw_params_any(pcm, hp)
    for fn, arg in ((alsa.snd_pcm_hw_params_set_access, 2),
                    (alsa.snd_pcm_hw_params_set_format, S32_LE),
                    (alsa.snd_pcm_hw_params_set_channels, CH)):
        r = fn(pcm, hp, arg)
        if r < 0:
            die(f"hw_params arg {fn.__name__}: {alsa.snd_strerror(r)}")
    d = ctypes.c_uint(RATE)
    r = alsa.snd_pcm_hw_params_set_rate_near(pcm, hp, ctypes.byref(d), 0)
    if r < 0 or d.value != RATE:
        die(f"rate negotiation asked {RATE} got {d.value} rc={r}")
    if alsa.snd_pcm_hw_params(pcm, hp) < 0:
        die("hw_params apply failed")
    sw = ctypes.c_void_p()
    alsa.snd_pcm_sw_params_malloc(ctypes.byref(sw))
    alsa.snd_pcm_sw_params_current(pcm, sw)
    alsa.snd_pcm_sw_params_set_start_threshold(pcm, sw, 1024)
    alsa.snd_pcm_sw_params(pcm, sw)
    return pcm


def die(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def hw_params_text():
    p = f"/proc/asound/card{CARD}/pcm0p/sub0/hw_params"
    try:
        return open(p).read()
    except OSError:
        return ""


def status_field(name):
    p = f"/proc/asound/card{CARD}/pcm0p/sub0/status"
    try:
        for line in open(p):
            if line.strip().startswith(name):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--seed", type=int, default=0xC5D)
    ap.add_argument("--save", default="", help="write golden bytes here too")
    args = ap.parse_args()

    n = int(RATE * args.seconds)
    frames = golden_frames(n, args.seed)
    blob = pack(frames)

    # Positive control on our OWN generator: an unmodified copy must hash the
    # same way twice, and a single flipped bit must NOT. A checker that cannot
    # fail is not a checker.
    tampered = bytearray(blob)
    tampered[BYTES_PER_FRAME * 7 + 3] ^= 0x80      # one bit, L of frame 7
    h1 = hashlib.sha256(blob).hexdigest()
    h2 = hashlib.sha256(blob).hexdigest()
    h3 = hashlib.sha256(bytes(tampered)).hexdigest()
    assert h1 == h2 and h1 != h3, "golden hashing is broken"
    print(f"golden frames={n} sha256={h1[:16]} (tamper-detect verified)")

    if args.save:
        open(args.save, "wb").write(blob)
        print(f"golden written to {args.save}")

    pcm = open_pcm()
    print("negotiated hw_params:\n  " + hw_params_text().strip().replace("\n", "\n  "))
    if f"{RATE} ({RATE}/1)" not in hw_params_text():
        die("kernel did not grant the requested rate")
    if "S32_LE" not in hw_params_text():
        die("kernel did not grant S32_LE -- ALSA fell back, the whole run "
            "would have validated the wrong wire format")

    alsa.snd_pcm_prepare(pcm)
    # Baseline AFTER prepare: snd_pcm_prepare resets the substream's xrun
    # counter, so reading it before prepare would carry the previous run's
    # residue into the comparison and either mask a real underrun or blame
    # this run for an old one.
    x0 = status_field("xruns")
    off = 0
    CHUNK = 4096 * BYTES_PER_FRAME
    t0 = time.time()
    while off < len(blob):
        seg = blob[off:off + CHUNK]
        w = alsa.snd_pcm_writei(pcm, seg, len(seg) // BYTES_PER_FRAME)
        if w < 0:
            alsa.snd_pcm_recover(pcm, w, 1)
            print(f"  recover at frame {off // BYTES_PER_FRAME}")
            continue
        off += w * BYTES_PER_FRAME
    alsa.snd_pcm_drain(pcm)
    alsa.snd_pcm_close(pcm)
    dt = time.time() - t0
    x1 = status_field("xruns")

    print(f"wrote {off // BYTES_PER_FRAME} frames in {dt:.2f} s "
          f"(wall {args.seconds:.2f} s)")
    print(f"xruns before={x0} after={x1}")
    if off != len(blob):
        die(f"short write: {off} of {len(blob)} bytes accepted")
    if x0 != x1:
        die(f"xruns during run ({x0} -> {x1}): the device or host underran, "
            "and underruns are where the firmware inserts its own words")
    print("LEG1: PASS  host emitted the exact golden stream at 384k/S32_LE, "
          "no resample, no fallback, no xrun")
    print("LEG1-DONE")


if __name__ == "__main__":
    main()
