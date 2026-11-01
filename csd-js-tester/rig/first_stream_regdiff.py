#!/usr/bin/env python3
"""Diff the codec's own registers: first stream (tone) vs steady stream (clean).

concurrent_fork.py closed the fork on a cold load, in ONE stream:
  - mic hears a 1 kHz tone +23.4 dB over the no-stream floor
  - the same firmware records all five DAC-bound nodes of that SAME stream
    as absolute silence (peak -240 dBFS, self-checked with a real tone)
  - I2S deadline monitor: 392 348 LRCLK frames, ZERO violations, port
    timestamp delta exactly 32..32 ticks (lib_xua's own criterion)

The digital path is fully exonerated by the device's own instruments, so the
tone is generated at or after the ES9219Q. This rig gets the state A/B for
free: the SAME board is bad on its first stream and clean on later ones, so
first-vs-steady register state IS the experiment -- no factory comparison
needed (and the factory image cannot answer SCPI anyway).

Reads the whole register map over the proven quoted-base64 REGister:READ?
path (unquoted silently returns nothing -- that instrument bug already cost
this investigation one false conclusion). Chunked: the transport is MIDI.

Verdict rules:
  tone confirmed on leg 1 + registers differ first-vs-steady
      -> the differing bits ARE the mechanism; each one is either written by
         our boot/AudioHwConfig path (find the write and its first-time
         condition) or is chip state we trigger with a write elsewhere.
  tone confirmed + registers identical
      -> not state at all: it is the analog behaviour of the unmute/idle
         sequence (charge pump, reference ramp). Then the fix is TIMING in
         csd_AudioHwConfig_Mute/UnMute, and the ladder (factory +1.0,
         production +5.8, mixer +11.7..23 dB) is explained by how long our
         start sequence keeps the chip in the unmuted-idle window.
"""
from __future__ import annotations
import base64, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ears_matrix as EM

APP = os.path.dirname(HERE)
PY = f"{APP}/.venv/bin/python"

REGS = list(range(0x00, 0x41)) + [0x43, 0x47, 0x48, 0x49, 0x51, 0x53]


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def reg_read(addrs):
    """Chunked quoted-base64 read. Returns {addr: value} or None on failure."""
    out = {}
    for i in range(0, len(addrs), 16):
        chunk = addrs[i:i + 16]
        tok = base64.b64encode(bytes(chunk)).decode()
        s = scpi(f'REGister:READ? "{tok}"').strip().strip('"')
        try:
            vals = base64.b64decode(s, validate=True)
        except Exception:
            return None
        if len(vals) != len(chunk):
            return None
        out.update(dict(zip(chunk, vals)))
    return out


def main():
    idn = scpi("*IDN?")
    if "ClearSound" not in idn:
        print(f"ABORT: firmware is not ours ({idn!r})"); return 1
    print(f"LIVE BUILD: {idn}")
    dac = EM.find_dac_card(None)
    ears = EM.find_ears_card(None)
    tmp = EM.tempfile.mkdtemp(prefix="regdiff_")
    EM.ensure_capture_switch(ears)
    EM.set_volume(dac, EM.VOL_0DB, EM.VOL_0DB, tmp)

    Z = os.path.join(tmp, "z.wav"); EM.write_tone24(Z, 20.0, 0.0, 0.0)

    def mic_and_regs(tag, launch):
        """Mic capture starts FIRST, then the stream: the artifact lives in
        the first ~250 ms of the stream, so a capture that starts after the
        stream open misses it (measured: -57.5 dBFS, i.e. nothing)."""
        cap = os.path.join(tmp, f"{tag}.wav")
        EM.kill_orphan_arecords()
        cap_p = subprocess.Popen(
            ["arecord", "-D", f"hw:{ears},0", "-f", "S24_3LE", "-r", "48000",
             "-c", "2", "-d", "4", "-t", "wav", cap],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        time.sleep(0.5)                     # arecord streaming
        snaps = []
        ff = launch()                       # <-- the open we are measuring
        t0 = time.monotonic()
        for _ in range(3):
            snaps.append((time.monotonic() - t0, reg_read(REGS)))
            time.sleep(0.12)
        cap_p.wait()
        ff.terminate(); ff.wait(timeout=10)
        r = EM.analyse(cap, tones=(EM.TONE_HZ,))
        tone = max(r["L"].tone_db[1000.0], r["R"].tone_db[1000.0])
        ok = [s for s in snaps if s[1] is not None]
        print(f"  {tag:8s} mic 1kHz max {tone:+6.1f} dBFS   "
              f"snapshots ok {len(ok)}/3 at "
              + " ".join(f"{t:.2f}s" for t, _ in snaps))
        return tone, (ok[0][1] if ok else None)

    def launch():
        return subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
             "-i", Z, "-f", "alsa", "-ac", "2", f"plughw:{dac},0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("\n== leg 1: FIRST stream after load (tone expected) ==")
    tone1, reg1 = mic_and_regs("first", launch)
    time.sleep(1.5)

    print("\n== leg 2: later stream (clean control) ==")
    tone2, reg2 = mic_and_regs("steady", launch)

    print("\n== verdict ==")
    if reg1 is None or reg2 is None:
        print("  INSTRUMENT-FAIL: register read failed -- no conclusion.")
        return 2
    if tone1 - tone2 < 6.0:
        print(f"  GATE: tone not reproduced this run ({tone1:+.1f} vs {tone2:+.1f})"
              " -- bench state, not the diff. Re-run.")
        return 2
    diff = {a: (reg1[a], reg2[a]) for a in REGS if reg1[a] != reg2[a]}
    if diff:
        print(f"  TONE +{tone1 - tone2:.1f} dB with {len(diff)} registers"
              " differing first-vs-steady:")
        for a, (x, y) in sorted(diff.items()):
            print(f"    reg 0x{a:02X}: first=0x{x:02X}  steady=0x{y:02X}")
        print("  Those bits ARE the mechanism. Trace each to the write that"
              " causes it (boot, AudioHwConfig, volume apply, or chip state)."
              " Status regs (0x47-0x49, 0x51, 0x53) differing without a write"
              " = chip state machine: find the write that trips it.")
    else:
        print(f"  TONE +{tone1 - tone2:.1f} dB with ALL registers IDENTICAL"
              " first-vs-steady -> not codec STATE. It is the analog"
              " behaviour of the unmute sequence: the chip hears its own"
              " reference/charge-pump settle as a 1 kHz-ish transient. The"
              " ladder (factory +1.0, prod +5.8, mixer +11.7..23) is then"
              " explained by TIME SPENT unmuted-idle per stream start --"
              " every extra register write our start path adds lengthens the"
              " window between MUTE release and first real sample. Fix:"
              " shorten csd_AudioHwConfig / write before unmute, or hold"
              " mute until the first host frame is queued -- and the"
              " 150 ms hold that made A WORSE now makes sense: it lengthened"
              " exactly that window.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
