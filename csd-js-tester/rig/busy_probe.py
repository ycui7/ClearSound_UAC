#!/usr/bin/env python3
"""Tile[1] resource check: does mixer1 (with the generator inside it) come
close to the audio() thread's sample-period deadline?

Placement being measured (verified in main.xc, not assumed):

  tile[1]  mixer1  <-- generator NCO + per-source gain + (this instrument)
  tile[1]  XUA_AudioHub audio()  <-- I2S port servicing, HARD deadline
  tile[0]  decouple/USB ISO      <-- feeds mixer1 via c_mix_out
  tile[1]  SCPI-over-MIDI diag task

MIXer:BUSY? reports the peak ticks mixer1 spent between the request token
(unblocks audio()) and the completed sample exchange -- the exact window
audio() waits for us. 12500 ticks = one 48 kHz period at 600 MHz. Approaching
it = missed I2S deadline = the mechanism that also keeps decouple pre-filling
the token word, so this single number tests the contention theory of case A.

Legs per cold boot:
  A  zeros stream, generator OFF (product path)
  B  zeros stream, generator armed full-scale 1 kHz (worst-case mixer work)
  C  tone stream (real host content + generator, worst case overall)

Compare A vs B vs C. If C peaks where A does not, the generator + host mix
loads the shared tile to the edge, and any further per-frame work is unsafe.

Usage: python3 rig/busy_probe.py [stream_seconds]
"""
from __future__ import annotations
import os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ears_matrix as EM

APP = os.path.dirname(HERE)
PY = f"{APP}/.venv/bin/python"
SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
TICKS_PER_FRAME = 12500          # 48 kHz, 600 MHz fast mode


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def stream(wav, secs):
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
         "-i", wav, "-f", "alsa", "-ac", "2", f"plughw:{DAC},0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def leg(tag, wav, armed):
    scpi("DEBUG:MIXer:DACPeak:CLEAR")            # clears peaks AND busy peak
    f0 = int(scpi("DEBUG:MIXer:FRAMES?") or 0)
    ff = stream(wav, SECS)
    time.sleep(SECS)
    ff.terminate(); ff.wait(timeout=10)
    time.sleep(0.3)
    busy = int(scpi("DEBUG:MIXer:BUSY?") or 0)
    f1 = int(scpi("DEBUG:MIXer:FRAMES?") or 0)
    frames = f1 - f0
    pct = 100.0 * busy / TICKS_PER_FRAME
    print(f"  {tag:26s} busy peak {busy:6d} ticks ({pct:5.1f}% of a frame)"
          f"   frames {frames:7d}  gen={'ON' if armed else 'off'}")
    return busy


def main():
    global DAC
    idn = scpi("*IDN?")
    if "ClearSound" not in idn:
        print(f"ABORT: not ours ({idn!r})"); return 1
    print(f"LIVE BUILD: {idn}")
    DAC = EM.find_dac_card(None)
    tmp = EM.tempfile.mkdtemp(prefix="busy_")
    Z = os.path.join(tmp, "z.wav");   EM.write_tone24(Z, 10.0, 0.0, 0.0)
    T = os.path.join(tmp, "t.wav");   EM.write_tone24(T, 10.0, 0.5, 0.5)

    print("\n== legs (each clears the window, streams, reads peak) ==")
    a = leg("A zeros, gen off", Z, False)
    time.sleep(0.5)
    scpi("GENerator:SINe 48000,1000000,1073741824,0")
    b = leg("B zeros, gen 1k full", Z, True)
    scpi("GENerator:OFF")
    time.sleep(0.5)
    scpi("GENerator:SINe 48000,1000000,1073741824,0")
    c = leg("C tone + gen 1k full", T, True)
    scpi("GENerator:OFF")

    print("\n== verdict ==")
    worst = max(a, b, c)
    pct = 100.0 * worst / TICKS_PER_FRAME
    print(f"  worst peak {worst} ticks = {pct:.1f}% of one sample period")
    if pct < 25:
        print("  -> GENERATOR IS SAFE ON tile[1]: even full-scale armed NCO +"
              "\n     host tone keeps mixer1's audio-wait window far from the"
              "\n     deadline. Contention is not case A; do not move it off"
              "\n     the mixer thread.")
        return 0
    if pct < 60:
        print("  -> NOTICEABLE but with margin. Any new per-frame work on"
              "\n     mixer1 must re-run this probe before shipping.")
        return 0
    print("  -> DANGER: near the sample-period deadline. This is a real"
          "\n     resource conflict on the shared tile: missed I2S deadlines,"
          "\n     decouple prefill re-emitting the token word. Offload the"
          "\n     generator (audio() thread has slack only if DIAG taps are"
          "\n     off) or cut per-frame work.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
