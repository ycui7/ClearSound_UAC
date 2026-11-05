#!/usr/bin/env python3
"""Idle-power acceptance test (TODO item 7, owner directive 2026-11-04).

Proves the two things the owner named, using the codec's OWN registers rather
than a log line:

  R1  THE DAC REALLY WENT DOWN. The volume registers at the bottom of the idle
      window must differ from both the playing state and the woke state. A log
      line says "DAC muted + held in reset"; only the chip says whether it did.
  R2  VOLUME RETURNS. The level after wake equals the level before shutdown.
      This is the one a log cannot prove: the restore prints "volume restored"
      whether or not anything moved, and every volume write is DEDUPED against
      a cache of what the chip last held. A full re-init resets the CHIP behind
      that cache, so without invalidation the write is skipped as a no-op and
      the box wakes at default gain forever. Reading regs 3/15/16 out of the
      codec is the only direct evidence, and it is the bug this feature
      actually shipped with.

Register map (ES9219Q): 3 = analog volume, 15/16 = per-channel digital volume,
7 = FILTER_SHAPE/system-mute.

Why host volume is moved off 0 dB first: at the chip default a broken restore
is indistinguishable from a working one, because both read the same value. A
non-default level is what makes R2 able to fail.

Register access uses rig/automute_ab.py's helpers verbatim: the QUOTED base64
form, because the unquoted form returns nothing and silently voided an earlier
instrument in this repo.
"""
import base64
import os
import subprocess
import sys
import time

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(APP, ".venv", "bin", "python")
if not os.path.exists(PY):
    PY = sys.executable

DEV = "hw:3,0"
RATE = 384000
TONE = "/tmp/pw_tone.wav"
IDLE_WAIT = 55          # build used -DCSD_PW_IDLE_MS=20000: > 2x margin
CARD = 3                # the DAC's ALSA card, from `arecord -l`


def scpi(cmd, timeout=12):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def regs_read(addrs):
    """QUOTED base64 form -- the unquoted form returns nothing (the bug that
    voided the first automute instrument, per rig/automute_ab.py)."""
    tok = base64.b64encode(bytes(addrs)).decode()
    out = scpi('REGister:READ? "%s"' % tok).strip().strip('"')
    try:
        return list(base64.b64decode(out, validate=True))
    except Exception:
        return None


def regs():
    """{addr: value} for the five registers this test reads, in one round trip."""
    addrs = [3, 7, 15, 16]
    vals = regs_read(addrs)
    if vals is None or len(vals) != len(addrs):
        return None
    return dict(zip(addrs, vals))


def fmt(m):
    if m is None:
        return "<register read failed>"
    return "reg3=0x%02x reg7=0x%02x reg15=0x%02x reg16=0x%02x" % (
        m[3], m[7], m[15], m[16])


def play(seconds=2.0):
    subprocess.run(["aplay", "-D", DEV, "-f", "S32_LE", "-r", str(RATE),
                    "-c", "2", TONE],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=30)


# The card's playback controls are 'ClearSound DAC (UAC2.0) Playback
# Volume/Switch' (numids 5 and 6). There is NO 'Speaker' control on this
# card: the first version of this test used `sset Speaker`, which is a silent
# no-op, so the codec stayed at its 0 dB default and requirement R2 could not
# fail -- a broken restore and a working one both read reg3=0x40, reg15/16=0.
# That is precisely the trap this file's own header warns about, so the
# control is addressed by NUMID and the write is read back and asserted.
VOL_NUMIDS = (5, 6)


def set_volume(pct):
    """Set both playback volume numids, then prove they took."""
    for nid in VOL_NUMIDS:
        subprocess.run(["amixer", "-q", "-c", str(CARD), "cset",
                        "numid=%d" % nid, "%d%%" % pct],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["amixer", "-q", "-c", str(CARD), "cset",
                        "numid=%d" % (nid - 2), "on"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return host_volume()


def host_volume():
    """The first playback volume numid's raw value, or None.

    The control is INTEGER on 0..192 (0 dB at 192, -48 dB at 0, so 0.25
    dB/step). amixer's '%' form is a percentage OF THAT RANGE, so asking for
    40%% yields values=77 -- which is why the first version of this test
    asserted on the literal string '40%' in the readback and declared failure
    on a write that had actually worked. Assert on the parsed number.
    """
    import re as _re
    out = subprocess.run(["amixer", "-c", str(CARD), "cget",
                          "numid=%d" % VOL_NUMIDS[0]],
                         capture_output=True, text=True).stdout
    m = _re.search(r":\s*values=(\d+)", out)
    return int(m.group(1)) if m else None


def main():
    if not os.path.exists(TONE):
        print("NOT-PROVEN: missing %s -- make the tone first" % TONE)
        return 2

    print("device:", scpi("*IDN?", 15))

    # Non-default level so R2 is capable of failing.
    print("\n-- host volume to 40%% (away from 0 dB, so a broken restore can show)")
    set_volume(40)
    v = host_volume()
    print("   readback: numid %d values=%s (0..192, 192 = 0 dB)"
          % (VOL_NUMIDS[0], v))
    if v is None or v == 192:
        print("NOT-PROVEN: host volume still at 0 dB default; R2 cannot be "
              "judged (a default level hides a broken restore)")
        return 2
    play(1.0)

    t0 = regs()
    print("T0 playing, before idle   :", fmt(t0))
    if t0 is None:
        print("NOT-PROVEN: cannot read registers")
        return 2

    print("\n-- idle %d s (timeout 20 s)" % IDLE_WAIT)
    time.sleep(IDLE_WAIT)
    t1 = regs()
    print("T1 after power-down       :", fmt(t1))

    print("\n-- wake: play again")
    play(2.0)
    time.sleep(3.0)
    t2 = regs()
    print("T2 after wake             :", fmt(t2))

    print("\n-- second cycle (a restore that works once is not a restore)")
    time.sleep(IDLE_WAIT)
    t3 = regs()
    print("T3 after 2nd power-down   :", fmt(t3))
    play(2.0)
    time.sleep(3.0)
    t4 = regs()
    print("T4 after 2nd wake         :", fmt(t4))

    fails = []
    states = {"T0": t0, "T1": t1, "T2": t2, "T3": t3, "T4": t4}
    missing = [k for k, v in states.items() if v is None]
    if missing:
        print("NOT-PROVEN: register read failed at %s; cannot judge"
              % ", ".join(missing))
        return 2

    # R2: volume must return, every cycle.
    for name, before, after in (("cycle 1", t0, t2), ("cycle 2", t0, t4)):
        for r in (15, 16, 3):
            if before[r] != after[r]:
                fails.append("R2 %s: reg%d after wake is 0x%02x, was 0x%02x "
                             "before shutdown -- volume did not return"
                             % (name, r, after[r], before[r]))

    # R1: the down state must be a real down state, not just a log line.
    if (t1[15], t1[16]) == (t0[15], t0[16]):
        fails.append("R1: reg15/16 unchanged at T1 -- the volume was never "
                     "driven to the floor, so the down sequence did not run")
    if (t1[15], t1[16]) == (t2[15], t2[16]):
        fails.append("R1: reg15/16 identical at T1 (down) and T2 (awake) -- "
                     "the restore never moved the register")
    if (t3[15], t3[16]) == (t0[15], t0[16]):
        fails.append("R1: cycle 2 never powered down")

    print()
    if fails:
        for f in fails:
            print("FAIL:", f)
        return 1
    print("PASS: the codec really powered down and the volume came back, twice")
    print("      down reg15/16 = 0x%02x/0x%02x   awake = 0x%02x/0x%02x   before = 0x%02x/0x%02x"
          % (t1[15], t1[16], t2[15], t2[16], t0[15], t0[16]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
