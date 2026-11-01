#!/usr/bin/env python3
"""Cold-boot token forensics: what word does the prefill actually carry?

Source read (xua_audiohub.xc): `underflowWord` is a LOCAL of audio(), starts
0, and becomes 0xFA969600 / 0x96969696 ONLY when the shared `dsdMode` reads
DOP/NATIVE at audio() entry. A cold PCM open therefore prefill-emits ZERO --
which contradicts every '0x96969696 carried by the prefill' story told from
the node-4 TOKEN peak, unless dsdMode was non-zero at first entry (a shared
global surviving from a previous image's stream, or the mixer-side
csd_dsd_mode latch disagreeing with the hub's local copy).

This rig reads the three facts that settle it on ONE cold boot:

  MIXer:HWCfg?      how many times csd_AudioHwConfig ran, last (freq,mclk)
                    -> 'first stream' = the leg where this counter increments
  MIXer:DACPeak?    all 7 nodes incl. 4=TOKEN and 6=POSTDSP, with the RAW
                    WORD that set each peak. TOKEN peak==0 with a live mic
                    artifact exonerates the prefill path completely.
  mic 1 kHz         does the artifact exist on this boot at all

If TOKEN stays 0 while the artifact is present, stop every underflow-word
hypothesis: the word the decouple echoes is provably silence, and the only
remaining producer is the chip's analog side reacting to stream open itself.

Usage: python3 rig/first_stream_token.py
"""
from __future__ import annotations
import os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ears_matrix as EM

APP = os.path.dirname(HERE)
PY = f"{APP}/.venv/bin/python"
NODES = ("host", "postmix", "volume", "dac", "token", "predsp", "postdsp")
CAP_D = 3


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def parse_dacpeak():
    s = scpi("DEBUG:MIXer:DACPeak?")
    try:
        v = [int(x) for x in s.split(",")]
    except ValueError:
        return None
    if len(v) != 33:
        return None
    out = {n: tuple(v[4 * i:4 * i + 4]) for i, n in enumerate(NODES)}
    out["tail"] = tuple(v[28:])        # nzL, nzR, firstFrame, wL, wR
    return out


def hwcfg():
    try:
        return tuple(int(x) for x in scpi("DEBUG:MIXer:HWCfg?").split(","))
    except ValueError:
        return None


def main():
    idn = scpi("*IDN?")
    if "ClearSound" not in idn:
        print(f"ABORT: not ours ({idn!r})"); return 1
    print(f"LIVE BUILD: {idn}")
    dac = EM.find_dac_card(None)
    ears = EM.find_ears_card(None)
    tmp = EM.tempfile.mkdtemp(prefix="token_")
    EM.ensure_capture_switch(ears)
    EM.set_volume(dac, EM.VOL_0DB, EM.VOL_0DB, tmp)
    Z = os.path.join(tmp, "z.wav"); EM.write_tone24(Z, 20.0, 0.0, 0.0)

    h_boot = hwcfg()
    print(f"bring-up counter at boot: {h_boot}")

    # Arm the instrument, then the FIRST stream after this cold load.
    scpi("DEBUG:MIXer:DACPeak:CLEAR")
    cap = os.path.join(tmp, "first.wav")
    EM.kill_orphan_arecords()
    p = subprocess.Popen(
        ["arecord", "-D", f"hw:{ears},0", "-f", "S24_3LE", "-r", "48000",
         "-c", "2", "-d", str(CAP_D), "-t", "wav", cap],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    ff = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
         "-i", Z, "-f", "alsa", "-ac", "2", f"hw:{dac},0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(CAP_D)
    p.wait()
    ff.terminate(); ff.wait(timeout=10)

    pk = parse_dacpeak()
    h1 = hwcfg()
    r = EM.analyse(cap, tones=(EM.TONE_HZ,))
    tone = max(r["L"].tone_db[EM.TONE_HZ], r["R"].tone_db[EM.TONE_HZ])
    floor = min(r["L"].floor, r["R"].floor)
    print(f"\nfirst stream: mic 1kHz {tone:+6.1f}  floor {floor:6.1f}  "
          f"(artifact present: {tone - floor >= 6.0})")
    print(f"bring-up counter after first stream: {h1}   "
          f"(incremented: {h_boot is not None and h1 is not None and h1[0] > h_boot[0]})")
    if pk is None:
        print("INSTRUMENT-FAIL: MIXer:DACPeak? unreadable -- no conclusion.")
        return 2

    print("\nnode peaks (dBFS) and the raw word that set them:")
    import math
    for n in NODES:
        pl, pr, wl, wr = pk[n]
        m = max(pl, pr)                       # unsigned magnitude, Q31 FS
        db = -240.0 if m == 0 else 20.0 * math.log10(m / 2147483647.0)
        print(f"  {n:8s} peak {db:+7.1f} dBFS   wordL 0x{wl:08X}  wordR 0x{wr:08X}")
    print(f"  host non-zero: L {pk['tail'][0]}  R {pk['tail'][1]}   "
          f"first frame {pk['tail'][2]}  first words 0x{pk['tail'][3]:08X} "
          f"0x{pk['tail'][4]:08X}")

    tok = max(pk["token"][0], pk["token"][1])
    pd = max(pk["predsp"][0], pk["predsp"][1])
    po = max(pk["postdsp"][0], pk["postdsp"][1])
    print("\n== verdict ==")
    if tone - floor < 6.0:
        print("  no artifact this boot -- re-run (case A is deterministic "
              "over boots, so a quiet boot is a bench question, not evidence)")
        return 2
    if tok == 0 and pd == 0 and po == 0:
        print("  TOKEN/PREDSP/POSTDSP all ZERO while the mic hears the "
              "artifact: the word the decouple echoes during prefill is "
              "PROVABLE silence. Every underflow-word / prefill-content "
              "hypothesis is dead, including my own prefill fix. The "
              "producer is the chip's analog side reacting to stream open.")
        return 1
    print(f"  non-zero content on the prefill/DAC path: token={tok:#x} "
          f"predsp={pd:#x} postdsp={po:#x} -> follow the raw words above "
          "to the node where it first appears.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
