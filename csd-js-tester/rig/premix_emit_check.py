#!/usr/bin/env python3
"""Does the mixer thread's pre-mixer hook emit? Self-contained, and this
time the two invalidating bugs of the first version are fixed and defended
against:

  * the SCPI frequency unit is MILLIHERTZ -- 1 kHz is 1_000_000, not 1e9.
    Passing 1e9 asked for 1 MHz, the device rejected it above Nyquist, and
    the script then reported "hook did not emit" about a generator that was
    never armed. The arm reply is now checked.
  * the ALSA card must be the NUMBER from /proc/asound/cards. The first
    version sliced out the card LABEL (with trailing spaces), so ffmpeg
    never opened the endpoint. With no host stream mixer1 exchanges no
    samples at all, so the mixer thread cannot run, which is not a
    generator fault and must not be reported as one.

Also prints dsd_flag: the previous run showed 2 (native DSD) at rest, which
would shut the gate legitimately -- that has to be seen, not assumed away.
"""
import os
import re
import subprocess
import sys
import time

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
AMP_FULL = 1073741824          # ~0 dBFS Q31
FREQ_MHZ_1K = 1000 * 1000      # 1 kHz in the device's milliHz unit


def scpi(cmd, timeout=8, retries=4, pause=1.0):
    """One command, with retry on EMPTY reply.

    An empty reply is not a rejection: it means no SysEx came back inside the
    timeout. That reproduces when the first command lands while the device is
    still settling after stream start (the proven rig arms only several
    seconds and several queries into a session, and never hits this). An ERROR
    reply is a real answer and is returned without retry -- confusing
    'no reply' with 'rejected' would hide a genuine parameter fault behind a
    retry loop.
    """
    last = ""
    for attempt in range(retries):
        rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                             "--timeout", str(timeout), cmd],
                            capture_output=True, text=True, cwd=APP,
                            timeout=timeout + 10)
        last = (rr.stdout or "").strip()
        if last:
            return last
        if attempt == 0 and (rr.stderr or "").strip():
            print(f"    [{cmd}] stderr: {rr.stderr.strip()[:120]}")
        time.sleep(pause)
    return last


def card_number(pat):
    """Card NUMBER (the integer in brackets), not the label."""
    for line in open("/proc/asound/cards"):
        if pat in line.lower():
            m = re.search(r"^\s*(\d+)\s*\[", line)
            if m:
                return m.group(1)
    return None


XMOS = os.environ.get("CSD_XMOS_CARD") or card_number("xmos")
print("XMOS playback card number:", repr(XMOS))
if not XMOS:
    sys.exit("no XMOS card -- nothing to measure")

stream = subprocess.Popen(
    ["sg", "audio", "-c",
     f"ffmpeg -v error -f lavfi -i anullsrc=r=48000:cl=stereo -t 60 "
     f"-f alsa hw:{XMOS}"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2.5)
if stream.poll() is not None:
    sys.exit(f"STREAM DIED (rc={stream.poll()}) -- mixer1 would never run; "
             "this is a rig fault, not a generator fault")
print("stream alive: pid", stream.pid)


def counts(label):
    c = scpi("GENerator:COUNts?")
    print(f"  counters [{label}]: {c}")
    return [int(x) for x in re.findall(r"-?\d+", c)]


def state(label):
    s = scpi("GENerator:STATe?")
    print(f"  state [{label}]: {s}")
    return s


print("\n-- before arm --")
state("boot")
c0 = counts("boot")

r = scpi(f"GENerator:SINe 48000,{FREQ_MHZ_1K},{AMP_FULL}")
print("\narm reply:", repr(r))
# An EMPTY reply is normal for the arm commands on this device: the rig's
# own arm_and_adopt never inspects it -- success is proven by GENerator:
# STATe? moving to SINE, and rejection is proven by an ERROR reply (the rig
# asserts 'ERROR -224' strings for bad arguments). The first version of this
# script treated empty as rejection and exited on a generator that was, in
# fact, armed -- the same 'no reply is not a rejection' trap the scpi retry
# comment above records. Only an ERROR reply means rejected.
if r.upper().startswith("ERROR"):
    scpi("GENerator:OFF")
    stream.terminate()
    sys.exit(f"ARM REJECTED ({r!r}) -- generator never armed; counter "
             "deltas would be meaningless")

time.sleep(0.5)
st = state("armed")
if not st.startswith("1,"):          # 1 = SINE
    scpi("GENerator:OFF")
    stream.terminate()
    sys.exit(f"NOT ARMED (state={st!r}) -- cannot judge emission")

time.sleep(2.0)
c1 = counts("2 s armed")
scpi("GENerator:OFF")
time.sleep(0.3)
counts("after OFF")

d = [y - x for x, y in zip(c0[:3], c1[:3])]
print("\nDELTA while armed: emit=%d sup_dsd=%d sup_dop=%d" % tuple(d))
print("dsd_flag=%d dop_latched=%d (boot reading, before any arm)"
      % (c0[3] if len(c0) > 3 else -1, c0[4] if len(c0) > 4 else -1))
if d[0] > 5000:
    print("VERDICT: MIXER THREAD EMITTED -- injection works; any silence is "
          "downstream (mixer map / volume)")
elif d[1] + d[2] > 5000:
    print("VERDICT: hook RAN, gate SHUT it (dsd/dop) -- check why the "
          "stream presents as DSD")
else:
    print("VERDICT: hook did NOT run per-frame -- mixer branch not reached")

stream.terminate()
try:
    stream.wait(timeout=5)
except subprocess.TimeoutExpired:
    stream.kill()
