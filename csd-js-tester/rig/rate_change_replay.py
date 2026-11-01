#!/usr/bin/env python3
"""Does the case-A tone follow AudioHwConfig(), or follow "first stream"?

lib_xua runs the user bring-up (AudioHwConfig_Mute -> AudioHwConfig ->
AudioHwConfig_UnMute) only when the sample rate changes or on the very first
stream (xua_audiohub.xc `firstRun` / the rate-change block). Every later
stream at the same rate skips it -- and the rig sees exactly that: the first
stream after a load produces a 1 kHz tone at -33 dBFS, the second reads the
floor. That correlation is the whole remaining suspect list in one line.

The ladder says the tone's size tracks how much we do inside that window:

  factory image      +1.0 dB     (no CSD bring-up at all)
  mixer only         -52 dB      (no generator, no DSP: PASS)
  mixer + dsp        -51 dB      (PASS)
  mixer + gen        -33 dB      (FAIL; our bring-up is richest here)
  production         +5.8 dB     (FAIL)

If the tone is produced by the chip's reaction to our bring-up sequence, it
must reappear on a FORCED re-config at any time -- a 48k -> 44.1k -> 48k rate
change runs the identical path without a cold load. If it does not, the
trigger is genuinely first-open-only (USB/decouple prefill, DPLL cold start)
and the bring-up theory is dead.

This rig is the discriminator, and it is also the productivity unlock: a
repeatable trigger means bisecting chip writes against the mic in ONE boot
instead of one boot per hypothesis.

Legs (all in one cold boot, zeros only, E.A.R.S. card, 0 dBFS volume):

  1  first stream @48k        expect tone      (the gate's case A)
  2  stream @48k again        expect clean     (no re-config: control)
  3  stream @44.1k            re-config #1     <-- tests the theory
  4  stream @48k              re-config #2     <-- tests reproducibility
  5  stream @48k again        expect clean     (no re-config: control)

Usage: python3 rig/rate_change_replay.py
"""
from __future__ import annotations
import os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ears_matrix as EM

APP = os.path.dirname(HERE)
PY = f"{APP}/.venv/bin/python"
CAP_D = 4           # arecord seconds; artifact lives in the first ~250 ms
RATE = 48000


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def stream(rate, secs):
    """ffmpeg zeros at `rate` for `secs`, in a Popen we control."""
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
         "-f", "lavfi", "-i", f"anullsrc=r={rate}:cl=stereo",
         "-t", str(secs), "-c:a", "pcm_s24le", "-f", "alsa",
         f"plughw:{DAC},0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def leg(tag, rate):
    """Mic capture FIRST (the artifact is the first ~250 ms of the open)."""
    cap = os.path.join(TMP, f"{tag}.wav")
    EM.kill_orphan_arecords()
    p = subprocess.Popen(
        ["arecord", "-D", f"hw:{EARS},0", "-f", "S24_3LE", "-r", str(RATE),
         "-c", "2", "-d", str(CAP_D), "-t", "wav", cap],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    ff = stream(rate, CAP_D + 0.5)
    p.wait()
    ff.terminate(); ff.wait(timeout=10)
    r = EM.analyse(cap, tones=(EM.TONE_HZ,))
    tone = max(r["L"].tone_db[EM.TONE_HZ], r["R"].tone_db[EM.TONE_HZ])
    floor = min(r["L"].floor, r["R"].floor)
    print(f"  {tag:22s} @{rate:6d}  tone {tone:+6.1f}  floor {floor:7.1f}  "
          f"delta {tone - floor:+5.1f} dB")
    return tone, floor


def main():
    global DAC, EARS, TMP
    idn = scpi("*IDN?")
    if "ClearSound" not in idn:
        print(f"ABORT: firmware is not ours ({idn!r})"); return 1
    print(f"LIVE BUILD: {idn}")
    DAC = EM.find_dac_card(None)
    EARS = EM.find_ears_card(None)
    TMP = EM.tempfile.mkdtemp(prefix="replay_")
    EM.ensure_capture_switch(EARS)
    EM.set_volume(DAC, EM.VOL_0DB, EM.VOL_0DB, TMP)

    print("\n== legs (zeros, mic-first, one cold boot) ==")
    t1, f1 = leg("1-first-48k", RATE)
    time.sleep(1.0)
    t2, _ = leg("2-second-48k", RATE)
    time.sleep(1.0)
    t3, _ = leg("3-reconfig-44.1k", 44100)
    time.sleep(1.0)
    t4, _ = leg("4-reconfig-48k", RATE)
    time.sleep(1.0)
    t5, _ = leg("5-again-48k", RATE)

    print("\n== verdict ==")
    first_has = (t1 - f1) >= 6.0
    # A re-config leg counts as "carrying the tone" when it sits with the
    # first stream, well above the two no-re-config controls.
    ctrl = max(t2, t5)
    r3, r4 = t3 - ctrl, t4 - ctrl
    print(f"  first stream  {t1:+.1f}  ({'TONE' if first_has else 'clean'})")
    print(f"  controls      {t2:+.1f} {t5:+.1f}   (same rate, no re-config)")
    print(f"  re-configs    {t3:+.1f} ({r3:+.1f} vs ctrl)   "
          f"{t4:+.1f} ({r4:+.1f} vs ctrl)")
    if first_has and min(r3, r4) >= 6.0:
        print("  -> TRIGGER IS AudioHwConfig(): the tone rides EVERY re-config."
              "\n     The bring-up sequence (sw_pll_fixed_clock -> DSP seed ->"
              "\n     LED -> ES9219Q writes, then the UnMute RESYNC) is the"
              "\n     producer. Bisect those writes against the mic in ONE"
              "\n     boot via rate changes.")
        return 1
    if first_has and max(r3, r4) < 6.0:
        print("  -> NOT the bring-up sequence: re-configs are CLEAN while the"
              "\n     first stream is not. The trigger is first-open-only"
              "\n     (USB prefill / chip cold start), and it is gated by"
              "\n     CSD_GEN_EN per the build bisect.")
        return 1
    print("  -> GATE: case A did not reproduce this boot"
          f" (first {t1:+.1f} vs floor {f1:+.1f}) -- bench state, re-run.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
