#!/usr/bin/env python3
"""Prove I2S data reaches the ES9219Q, without any microphone.

The E.A.R.S. acoustic matrix (rig/ears_matrix.py) is the acceptance gate and
nothing replaces it. This is the interim evidence for the one link in the chain
that acoustics alone cannot separate out: whether the codec is actually being
fed. It was explicitly sanctioned in the project notes as the fallback while
the acoustic rig is blocked ("a codec-status poller over I2C (automute/DPLL
bits during stream) can prove I2S data reaches the ES9219Q without any
acoustics").

What it does prove: the DPLL is LOCKED only when a valid data stream is
arriving on the I2S pins. Lock during playback and loss of lock on a silent
transport is real evidence that samples -- not just a clock -- traverse the
bus. Read via REGister:READ? over MIDI, the same path the acceptance matrix
uses.

What it does NOT prove: that the analog output is audible, correct, or free of
pop/noise, or that a preset changed the sound. Those need the mic. Print this
alongside the acoustic result, never instead of it.

Registers (extensions/es9219q.h):
  0x40 CHIP_STATUS       chip_id<<2 | automute(bits 1:0) | dpll_lock(bit 0)
  0x48 READ_INPUT_SEL_N_AUTOMUTE_STAT
  0x4D READ_LOCK_STATUS
"""
import argparse
import base64
import subprocess
import sys
import time

CHIP_STATUS = 0x40
AUTOMUTE_STAT = 0x48
LOCK_STATUS = 0x4D
REGS = [CHIP_STATUS, AUTOMUTE_STAT, LOCK_STATUS]

# Resolve the app root from this file, not an absolute path: the tree
# lives inside the firmware repo (csd-js-tester/rig/ sits next to
# csd_diag.py) and the old scratch copy must not be baked in.
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = APP + "/.venv/bin/python"


def reg_read(addrs):
    """REGister:READ? with base64 payload, the BATMCU convention.

    Replies with raw base64 (no quotes -- SCPI_ResultCharacters), which is why
    the decoder is diffed against the Python stdlib rather than trusted.
    """
    payload = base64.b64encode(bytes(addrs)).decode()
    out = subprocess.run(
        [PY, "csd_diag.py", "--backend", "rawmidi", "--timeout", "6",
         f'REGister:READ? "{payload}"'],
        capture_output=True, text=True, cwd=APP)
    if out.returncode != 0:
        raise SystemExit(f"REGister:READ? failed rc={out.returncode}: "
                         f"{(out.stderr or out.stdout).strip()[:300]}")
    line = [l.strip() for l in out.stdout.splitlines() if l.strip()]
    if not line:
        raise SystemExit("REGister:READ? returned an empty reply")
    try:
        return list(base64.b64decode(line[-1], validate=True))
    except Exception as e:
        raise SystemExit(f"reply {line[-1]!r} is not valid base64: {e}")


def decode(vals):
    chip, automute, lock = vals[0], vals[1], vals[2]
    return {"chip_id": chip >> 2, "automute": chip & 0x03,
            "dpll_lock": chip & 0x01, "raw": vals,
            "automute_stat": automute, "lock_status": lock}


def play(dac, secs, tone):
    """Start playback non-blocking so registers can be polled DURING the
    stream -- polling after it ends measures an idle codec and proves nothing.
    """
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-i", tone,
         "-f", "alsa", "-ac", "2", f"plughw:{dac},0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dac", type=int, default=3)
    ap.add_argument("--tone", default="/tmp/tone48k.wav")
    ap.add_argument("--secs", type=int, default=4)
    a = ap.parse_args()

    # Zeros file as the control. Lock alone cannot prove data flows: the XMOS
    # I2S master can run BCLK/LRCLK continuously while the codec is powered,
    # which locks a DPLL with nothing but clock on the bus. Only a state that
    # CHANGES between a real signal and all-zero samples separates "data is
    # arriving" from "a clock is ticking".
    zeros = "/tmp/codec_zeros.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"anullsrc=r=48000:cl=stereo", "-t", str(a.secs + 1),
                    "-c:a", "pcm_s16le", "-y", zeros],
                   capture_output=True)

    def poll(n, label, proc=None):
        rows = [decode(reg_read(REGS)) for _ in range(n)]
        if proc is not None:
            proc.wait()
        time.sleep(0.7)
        print(f"  {label:14s} lock={[r['dpll_lock'] for r in rows]} "
              f"automute={[r['automute'] for r in rows]} "
              f"lock_status={[r['lock_status'] for r in rows]} "
              f"(chip_id={rows[0]['chip_id']})")
        return rows

    print("polling ES9219Q 0x40/0x48/0x4D over REGister:READ?")
    idle = poll(3, "idle")
    p_tone = play(a.dac, a.secs, a.tone)
    time.sleep(1.2)
    tone = poll(4, "tone playing", p_tone)
    p_zeros = play(a.dac, a.secs, zeros)
    time.sleep(1.2)
    zero = poll(4, "zeros playing", p_zeros)
    stopped = poll(3, "stopped")

    ok = True
    if idle[0]["chip_id"] == 0:
        print("FAIL: chip id reads 0 -- I2C is not answering, so no other "
              "reading here means anything.")
        return 1

    def any_lock(rows): return any(r["dpll_lock"] for r in rows)
    def any_unmute(rows): return any(r["automute"] == 0 for r in rows)
    def all_mute(rows): return all(r["automute"] != 0 for r in rows)

    lock_discriminates = any_lock(tone) and not any_lock(idle)
    mute_discriminates = any_unmute(tone) and all_mute(zero)

    if lock_discriminates:
        print("  DPLL: locks under stream, unlocked at idle -> discriminates.")
    else:
        print("  DPLL: does NOT discriminate (it is not 0 when it should be). "
              "The lock bit tracks the I2S clock, not the data, so on its own "
              "it proves nothing about samples reaching the codec.")
    if mute_discriminates:
        print("  automute: releases on a real tone, stays asserted on zeros -> "
              "discriminates. This is the evidence that DATA arrives.")
    else:
        print("  automute: no release/mute transition between tone and zeros.")

    if mute_discriminates or lock_discriminates:
        print("\nI2S->codec path shows live behaviour. Interim evidence only.")
    else:
        print("\nINCONCLUSIVE: neither the DPLL nor automute changed with the "
              "signal, so nothing here proves data reaches the codec. Do not "
              "read the constant '1's as health -- a flat line is the absence "
              "of evidence, not evidence of health.")
        ok = False
    print("This is NOT the acoustic gate. The firmware is still unqualified "
          "until rig/ears_matrix.py prints PASS.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
