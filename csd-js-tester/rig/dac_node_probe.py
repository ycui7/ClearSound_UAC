#!/usr/bin/env python3
"""THE fork in the case-A investigation, measured at the node that matters.

Facts so far (all cold loads, same mic/rig; rig/first_stream_arm.py,
rig/case_a_repeat.sh):

  factory image      +1.0 dB over no-stream   PASS
  production         +5.8 dB                  borderline
  mixer, gen OFF     +3.5 .. +6.8              borderline
  mixer, gen ON      +11.7 / +12.9             FAIL
  later streams, same gains as the FAIL leg: -1.4 dB (clean)

So the artifact is real, firmware-dependent, and confined to the FIRST
stream after load. The unresolved question has two answers that need
different fixes:

  (a) the firmware hands the DAC 1 kHz content in its first frames -> fix the
      module with the uninitialised state;
  (b) the words are silence and the codec itself rings when AudioHwConfig
      re-pokes it at stream open -> fix the bring-up order.

This rig decides it, digitally, with no mic in the loop: the firmware peaks
|sample| AT the GiveSamplesToDevice node -- the words it hands to I2S -- and
reports them over SCPI (MIXer:DACPeak?, MIXer:DACPeak:CLEAR, MIXer:FRAMES?).
The board's USB capture mirror cannot do this job in a mixer build: capture
is assembled by mixer1's GiveSamplesToHost, so the DIAG_LOOP hook that
served on the production build reads silence here (measured: -144 dBFS with
a real tone streamed).

Run against a FRESH load so the stream measured is the first one:
    run_loaded.sh rig/dac_node_probe.py
"""
import os, subprocess, sys, time, wave

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
RATE, SECS = 48000, 3
FS = float(1 << 31)


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


NODES = ("host", "postmix", "volume", "dac")

def peak():
    """16 values: per node (host, post-mix, post-volume, DAC hand-off) ->
    peakL, peakR, wordL, wordR. Returns {node: (peakL,peakR,wordL,wordR)}."""
    s = scpi("MIXer:DACPeak?")
    try:
        v = [int(x) for x in s.split(",")]
    except ValueError:
        return None
    if len(v) != 16 or min(v[:8]) < 0:
        return None
    return {n: tuple(v[4 * i:4 * i + 4]) for i, n in enumerate(NODES)}


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


def main():
    idn = scpi("*IDN?")
    if "ClearSound" not in idn:
        print(f"ABORT: firmware is not ours ({idn!r})"); return 1
    print(f"LIVE BUILD: {idn}")
    xm = card("xmos", "clearsound")
    if not xm:
        sys.exit("XMOS card not found")

    Z = os.path.join(os.environ.get("TMPDIR", "/tmp"), "dn_zeros.wav")
    with wave.open(Z, "wb") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
        w.writeframes(b"\x00" * (3 * 2 * int(RATE * (SECS + 1))))
    T = os.path.join(os.environ.get("TMPDIR", "/tmp"), "dn_tone.wav")
    import math
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
            print(f"  {tag:22s} INSTRUMENT-FAIL (DacPeak={p} frames={f0}->{f1})")
            return None
        print(f"  {tag:22s} frames +{f1 - f0:6d}  played={ok}")
        for n in NODES:
            pl, pr, wl, wr = p[n]
            db = max(20 * math.log10(max(pl, pr) / FS + 1e-12), -240.0)
            print(f"      {n:8s} peakL {pl:11d}  peakR {pr:11d}  "
                  f"({db:+7.1f} dBFS)  words 0x{wl & 0xFFFFFFFF:08X}"
                  f"/0x{wr & 0xFFFFFFFF:08X}")
        return {n: max(20 * math.log10(max(p[n][0], p[n][1]) / FS + 1e-12), -240.0)
                for n in NODES}, p

    print("\n== leg 1: FIRST stream this board has ever opened = host zeros ==")
    d1, w1 = leg("first zeros", Z)
    print("\n== leg 2: second zeros stream (control: clean per the mic) ==")
    d2, _ = leg("second zeros", Z)
    print("\n== leg 3: SELF-CHECK, real 1 kHz tone (peak must be ~ -2 dBFS) ==")
    d3, _ = leg("tone", T)

    print("\n== verdict ==")
    if d1 is None or d3 is None:
        print("  INSTRUMENT-FAIL: no conclusion.")
        return 2
    if d3["dac"] < -6.0:
        print(f"  SELF-CHECK FAIL: tone peaked only {d3['dac']:.1f} dBFS at the DAC"
              " node -- the hook is not seeing the samples it claims. No"
              " conclusion; do not believe any number above.")
        return 2
    dirty = [n for n in NODES if d1[n] > -40.0]
    if not dirty:
        print(f"  ANALOG: every node on the path peaks at {max(d1.values()):.1f} dBFS"
              " (silence) on the first zeros stream while the mic hears +11.7 dB"
              " over the floor. The mixer output is clean, so the tone is made"
              " at or after the codec -- look at what AudioHwConfig does at"
              " stream open, not at the mixer's arithmetic.")
        return 0
    clean = [n for n in NODES if n not in dirty]
    first = dirty[0]
    src = ("the host exchange itself (nothing in our code)" if first == "host"
           else f"between {clean[-1] if clean else 'the host'} and {first}")
    print(f"  DIGITAL, LOCALISED: on the first zeros stream the word is already"
          f" non-zero at node '{first}' ({d1[first]:.1f} dBFS) and clean at"
          f" {clean or 'none'}. Origin: {src}.")
    print(f"  peak words there: L 0x{w1[first][2] & 0xFFFFFFFF:08X}"
          f"  R 0x{w1[first][3] & 0xFFFFFFFF:08X}"
          f"   (ASCII-looking = uninitialised static RAM)")
    if d2 and max(d2.values()) < -40.0:
        print("  Second stream is clean at every node -> first-stream-only, which"
              " is the signature of state that is never initialised and gets"
              " written on the first pass.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
