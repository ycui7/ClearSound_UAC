#!/usr/bin/env python3
"""Is the codec's DPLL correction the case-A tone? Trace 0x43 vs the mic.

Evidence stacked so far, all on cold loads, same rig:

  factory image      mic delta over the no-stream floor  +1.0 dB   PASS
  production build   +5.8 dB
  mixer build        +23.3 dB
  ...while the firmware's own peak instruments record SILENCE at all SEVEN
  nodes on both threads (host receive, post-mix, post-volume, mixer->audio,
  audio-thread pre-DSP, audio-thread post-DSP = the I2S-bound words) and the
  I2S deadline monitor reports 392,470 LRCLK frames with ZERO violations and
  a perfect 32-tick cadence.

No sample word anywhere in our firmware is non-zero and the wire is clean,
yet the mic hears 1 kHz. The last thing that moves is INSIDE the codec:
first_stream_regdiff.py found register 0x43 (DPLL_NUM) = 0xFE while the tone
was audible and 0x00 on the clean steady stream -- the chip's own DPLL
correction word.

The chip's MCLK is our software PLL. lib_xua ships an anti-glitch pause for
exactly this moment -- xua_audiohub.xc:965 "Allow some time for mclk to lock
and MCLK to stabilise - this is important to avoid glitches at start of
stream" -- but it sits inside #if (ADJUSTABLE_MCLK_REQUIRED), which this
board does not set, so for us the chip is unmuted while its DPLL still
chases.

This rig measures, it does not fix: cold load -> mic capture starts -> first
zero stream opens -> poll 0x43 + full register diff at POLL_MS through the
whole audible window. If the tone lives only while 0x43 is away from its
settled value, the fix is a poll on the chip's real lock in
AudioHwConfig_UnMute (the official delay, made conditional on the chip
instead of the clock). If 0x43 never leaves 0x00, the DPLL is not it.

Usage: python3 rig/dpll_trace.py [poll_ms]
"""
from __future__ import annotations
import base64, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ears_matrix as EM

APP = os.path.dirname(HERE)
PY = f"{APP}/.venv/bin/python"

POLL_MS = int(sys.argv[1]) if len(sys.argv) > 1 else 25
CAP_D = 3                       # arecord duration, seconds
# Watch list, keyed to es9219q.h (do NOT trust label memory -- an earlier
# version of this list called 0x08 the automute status; it is GPIO12_CONFIG):
#   0x42/0x43/0x44/0x45  DPLL correction words (they CHASE continuously in
#                        both legs: measured duty 38-59% first vs 41-54%
#                        steady, i.e. no discriminator -- see the verdict)
#   0x48  READ_INPUT_SEL_N_AUTOMUTE_STAT  bits 7..4 = OC_R, OC_L,
#         AUTOMUTE_R, AUTOMUTE_L  (what the csd_AudioHwConfig banner prints)
#   0x49  READ_LOCK_STATUS  MQA_LOCK / PLL_LOCK / ASRC
#   0x1B/0x1C/0x1A/0x0E/0x0F  clock/ASRC config, static so far
#   0x07  filter shape + system mute, 0x48's companion on the mute path
# The surviving hypothesis is chip-internal generation: seven firmware nodes
# prove the I2S-bound words are zero, yet the mic hears the artifact, and it
# is first-stream-only. If the analog stage were swinging hard we would see
# OC_/AUTOMUTE_ bits in 0x48 -- measured 0x01/0x02 (bits 4..7 clear) on BOTH
# legs, so no over-current and no automute event.
# FULL=1 scans the whole readback set from first_stream_regdiff.
REGS = ([0x43, 0x42, 0x44, 0x45, 0x1B, 0x1C, 0x18, 0x19, 0x1A, 0x0E, 0x07,
         0x48, 0x49]
        if not os.environ.get("FULL") else
        list(range(0x00, 0x41)) + [0x43, 0x47, 0x48, 0x49, 0x51, 0x53])


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def reg_read(addrs):
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
    tmp = EM.tempfile.mkdtemp(prefix="dpll_")
    EM.ensure_capture_switch(ears)
    EM.set_volume(dac, EM.VOL_0DB, EM.VOL_0DB, tmp)
    Z = os.path.join(tmp, "z.wav"); EM.write_tone24(Z, 20.0, 0.0, 0.0)

    # A0: floor with no stream ever opened (cold-load baseline).
    EM.kill_orphan_arecords()
    a0 = os.path.join(tmp, "a0.wav")
    subprocess.run(["arecord", "-D", f"hw:{ears},0", "-f", "S24_3LE",
                    "-r", "48000", "-c", "2", "-d", "2", "-t", "wav", a0],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r0 = EM.analyse(a0, tones=(EM.TONE_HZ,))
    f0 = max(r0["L"].tone_db[1000.0], r0["R"].tone_db[1000.0])
    print(f"\n== A0 floor (no stream): 1kHz max {f0:+6.1f} dBFS ==")

    # First stream, mic first then ffmpeg (the artifact is the first ~250 ms).
    cap = os.path.join(tmp, "first.wav")
    cap_p = subprocess.Popen(
        ["arecord", "-D", f"hw:{ears},0", "-f", "S24_3LE", "-r", "48000",
         "-c", "2", "-d", str(CAP_D), "-t", "wav", cap],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    ff = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
         "-i", Z, "-f", "alsa", "-ac", "2", f"plughw:{dac},0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.monotonic()
    traj = []                       # (t, {addr: val}) while the stream runs
    while time.monotonic() - t0 < CAP_D - 0.4:
        traj.append((time.monotonic() - t0, reg_read(REGS)))
        time.sleep(POLL_MS / 1000.0)
    cap_p.wait()
    ff.terminate(); ff.wait(timeout=10)

    r1 = EM.analyse(cap, tones=(EM.TONE_HZ,))
    t1 = max(r1["L"].tone_db[1000.0], r1["R"].tone_db[1000.0])
    print(f"== FIRST zero stream:   1kHz max {t1:+6.1f} dBFS   "
          f"delta vs A0 {t1 - f0:+.1f} dB ==")

    ok = [(t, s) for t, s in traj if s is not None]
    print(f"\n== 0x43 (DPLL_NUM) trajectory: {len(ok)}/{len(traj)} good polls, "
          f"{POLL_MS} ms ==")
    cells = []
    prev = None
    for t, s in ok:
        v = s.get(0x43)
        if v != prev:
            cells.append(f"{t:.2f}s:{'NA' if v is None else '0x%02X' % v}")
            prev = v
    print("  changes: " + ("  ".join(cells) if cells else "none"))
    vals = [s[0x43] for _, s in ok if s.get(0x43) is not None]
    if vals:
        print("  distinct: " + " ".join("0x%02X" % v for v in sorted(set(vals))))

    print("\n== registers that moved at all during the audible window ==")
    moved = [a for a in REGS if len({s[a] for _, s in ok if s.get(a) is not None}) > 1]
    if not moved:
        print("  none")
    for a in moved:
        seq = []
        prev = None
        for t, s in ok:
            v = s.get(a)
            if v != prev:
                seq.append(f"{t:.2f}s:{'NA' if v is None else '0x%02X' % v}")
                prev = v
        print(f"  0x{a:02X}: " + "  ".join(seq))

    # Leg 2: SAME polling on a later stream. Without this, "0x43 differs"
    # from 3 snapshots 120 ms apart is sampling luck (0x43 sits at 0x00 about
    # 60% of polls while hunting), which is exactly how the DPLL line got
    # started. Compare duty cycles, never single reads.
    time.sleep(2.0)
    cap2 = os.path.join(tmp, "steady.wav")
    cap_p = subprocess.Popen(
        ["arecord", "-D", f"hw:{ears},0", "-f", "S24_3LE", "-r", "48000",
         "-c", "2", "-d", str(CAP_D), "-t", "wav", cap2],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    ff = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
         "-i", Z, "-f", "alsa", "-ac", "2", f"plughw:{dac},0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.monotonic()
    traj2 = []
    while time.monotonic() - t0 < CAP_D - 0.4:
        traj2.append((time.monotonic() - t0, reg_read(REGS)))
        time.sleep(POLL_MS / 1000.0)
    cap_p.wait()
    ff.terminate(); ff.wait(timeout=10)
    r2 = EM.analyse(cap2, tones=(EM.TONE_HZ,))
    t2 = max(r2["L"].tone_db[1000.0], r2["R"].tone_db[1000.0])
    ok2 = [(t, s) for t, s in traj2 if s is not None]
    print(f"== STEADY stream (control):  1kHz max {t2:+6.1f} dBFS   "
          f"{len(ok2)}/{len(traj2)} polls ==")

    def duty(ok, a):
        v = [s[a] for _, s in ok if s.get(a) is not None]
        if not v:
            return None
        nz = sum(1 for x in v if x not in (0, None))
        return nz, len(v), sorted(set(v))

    print("\n== non-zero duty, first vs steady (identical polling) ==")
    verdicts = {}
    for a in REGS:
        d1, d2 = duty(ok, a), duty(ok2, a)
        if d1 is None or d2 is None:
            continue
        if d1[0] == 0 and d2[0] == 0:
            continue
        f1, f2 = d1[0] / d1[1], d2[0] / d2[1]
        verdicts[a] = f1 - f2
        print(f"  0x{a:02X}: first {d1[0]}/{d1[1]} ({100*f1:3.0f}%)  "
              f"steady {d2[0]}/{d2[1]} ({100*f2:3.0f}%)  gap {100*(f1-f2):+3.0f}pp"
              f"   vals {['0x%02X' % v for v in d1[2]]}")

    print("\n== verdict ==")
    if t1 - f0 < 6.0:
        print(f"  GATE: no tone this run ({t1:+.1f} vs floor {f0:+.1f}) -- "
              "re-run, nothing to conclude.")
        return 2
    # A register only counts as a discriminator if its duty differs by more
    # than poll noise. 20 pp is far above the run-to-run spread seen here.
    disc = [a for a, g in verdicts.items() if abs(g) > 0.20]
    if disc:
        print("  DISCRIMINATOR(s): " + ", ".join("0x%02X" % a for a in disc) +
              " -- duty differs first-vs-steady by >20 pp while the tone"
              " differs by %.1f dB. Gate on that in AudioHwConfig_UnMute."
              % (t1 - t2))
        return 1
    if t1 - t2 < 6.0:
        print("  GATE: tone not reproduced first-vs-steady (%+.1f vs %+.1f)"
              " -- bench state, re-run." % (t1, t2))
        return 2
    print("  NO register discriminator: every moving register moves just as"
          " much on the CLEAN stream. The DPLL chase is normal operation, not"
          " the tone. Chip-state diff over the audible window is empty ->"
          " the artifact is not visible in the codec's own status at all.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
