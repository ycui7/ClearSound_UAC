#!/usr/bin/env python3
"""Localise the frozen 0x96969696 word on the DAC-bound path (mixer build).

case_a_repeat.sh + first_stream_arm.py established: the acoustic gate's case A
is a REAL, firmware-dependent artifact confined to the FIRST stream after
load (factory image +1.0 dB, production +5.8, mixer+gen +11.7 -- and the same
gains measured later read -1.4 dB). dac_node_probe.py proved it is DIGITAL:
the words handed to the DAC peak at +1.4 dBFS on a stream of host zeros,
frozen at 0x96969696, clean on every later stream.

0x96969696 is not random RAM: it is lib_xua's native-DSD underflow word
(xua_audiohub.xc:228). The audio thread's OUT-prefill token (the value
StartSampleTransfer sends to fire the decouple ISR, echoed by
handle_audio_request as the sample for every channel while outUnderflow) is
exactly that variable. So the candidates are:

  A  the audiohub entered audio() with a STALE dsdMode==NATIVE (it is a
     file-scope global that keeps its value across streams) -> it sent the
     DSD underflow word as the prefill sample for a PCM stream;
  B  the mixer/decouple substituted the word somewhere else.

This rig separates them: node 4 peaks the TRIGGER TOKEN mixer1 receives, and
the nz/first-frame tail says whether the leak is a bounded prefill burst
(tens of frames, the token's signature) or a continuous feed (millions).
"""
from __future__ import annotations
import math, os, subprocess, sys, wave

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
RATE, SECS = 48000, 3
FS = float(1 << 31)
NODES = ("host", "postmix", "volume", "dac", "token", "predsp", "postdsp")

DSD_NATIVE_WORD = 0x96969696
DSD_DOP_WORD = 0xFA969600


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def peak():
    s = scpi("MIXer:DACPeak?")
    try:
        v = [int(x) for x in s.split(",")]
    except ValueError:
        return None
    if len(v) != 33 or min(v[:28]) < 0:
        return None
    out = {n: tuple(v[4 * i:4 * i + 4]) for i, n in enumerate(NODES)}
    out["tail"] = tuple(v[28:])          # nzL, nzR, firstFrame, firstWordL, firstWordR
    return out


def frames():
    try:
        return int(scpi("MIXer:FRAMES?"))
    except ValueError:
        return None


def card(*pats):
    for line in open("/proc/asound/cards"):
        if any(p in line.lower() for p in pats):
            return line.split("]", 1)[0].strip().split()[0]
    return None


def dbfs(v):
    return max(20 * math.log10(v / FS + 1e-12), -240.0)


def main():
    idn = scpi("*IDN?")
    if "ClearSound" not in idn:
        print(f"ABORT: firmware is not ours ({idn!r})"); return 1
    print(f"LIVE BUILD: {idn}")
    xm = card("xmos")
    if not xm:
        sys.exit("XMOS card not found")

    tmp = os.environ.get("TMPDIR", "/tmp")
    Z = os.path.join(tmp, "dnz.wav")
    with wave.open(Z, "wb") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
        w.writeframes(b"\x00" * (3 * 2 * int(RATE * (SECS + 1))))
    T = os.path.join(tmp, "dnt.wav")
    with wave.open(T, "wb") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
        n = int(RATE * (SECS + 1))
        w.writeframes(b"".join(
            int(0.8 * (2 ** 23 - 1) * math.sin(2 * math.pi * 1000 * i / RATE))
            .to_bytes(3, "little", signed=True) * 2 for i in range(n)))

    def play(path):
        pr = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
             "-f", "alsa", "-ac", "2", f"plughw:{xm},0"],
            capture_output=True, text=True, timeout=60)
        if pr.returncode:
            print(f"  PLAYBACK-FAIL rc={pr.returncode}: {pr.stderr.strip()[:180]}")
        return pr.returncode == 0

    def leg(tag, wav):
        scpi("MIXer:DACPeak:CLEAR")
        f0 = frames()
        ok = play(wav)
        p = peak()
        f1 = frames()
        if p is None or f0 is None or f1 is None:
            print(f"  {tag:14s} INSTRUMENT-FAIL (peak={p} frames={f0}->{f1})")
            return None
        print(f"  {tag:14s} frames +{f1 - f0:6d}  played={ok}")
        for n in NODES:
            pl, pr, wl, wr = p[n]
            print(f"      {n:8s} peak {max(pl, pr):11d} ({dbfs(max(pl, pr)):+7.1f} dBFS)"
                  f"  words 0x{wl & 0xFFFFFFFF:08X}/0x{wr & 0xFFFFFFFF:08X}")
        p["nf"] = f1 - f0
        nzl, nzr, ff, wfl, wfr = p["tail"]
        print(f"      HOST non-zero words: L {nzl}  R {nzr}  first at frame "
              f"{ff}  words 0x{wfl & 0xFFFFFFFF:08X}/0x{wfr & 0xFFFFFFFF:08X}")
        return p

    print("\n== leg 1: FIRST stream ever opened = host zeros ==")
    p1 = leg("first zeros", Z)
    print("\n== leg 2: second zeros stream (control) ==")
    p2 = leg("second zeros", Z)
    print("\n== leg 3: SELF-CHECK real tone ==")
    p3 = leg("tone", T)

    print("\n== verdict ==")
    if p1 is None or p3 is None:
        print("  INSTRUMENT-FAIL"); return 2
    if max(p3["dac"][0], p3["dac"][1]) < 0.25 * FS:
        print("  SELF-CHECK FAIL: tone never reached the DAC node. No conclusion.")
        return 2
    nzl, nzr, ff, wfl, wfr = p1["tail"]
    tk = max(p1["token"][0], p1["token"][1])
    tot = p1["nf"] or 1
    print(f"  first-stream host leak: {nzl}+{nzr} non-zero words "
          f"(~{tot} frames exchanged), token peak {dbfs(tk):+.1f} dBFS")
    if tk > 0.25 * FS:
        print(f"  TOKEN carries the word -> the audiohub sent its underflowWord"
              f" as the OUT-prefill sample. token word 0x{p1['token'][2] & 0xFFFFFFFF:08X}"
              f" {'== native-DSD underflow word' if (p1['token'][2] & 0xFFFFFFFF) == DSD_NATIVE_WORD else '!= native-DSD word'}.")
        print("  FIX belongs in the audiohub/bring-up: the underflow word must"
              " be 0 for PCM, and dsdMode must not survive a stream as stale"
              " state that selects the DSD word.")
    else:
        print("  token is clean but the host node is not -> the substitution is"
              " downstream of the token (decouple or mixer receive path).")
    if max(p1["dac"][0], p1["dac"][1]) < 0.01 * FS:
        print("  DAC node clean -> nothing reaches the codec digitally here.")
        return 0
    print("  word reaches the DAC node on the first stream: that is the case-A"
          " stimulus. Zero it at the source above and the gate should pass.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
