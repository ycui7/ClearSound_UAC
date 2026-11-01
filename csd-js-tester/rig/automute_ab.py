#!/usr/bin/env python3
"""Automute A/B done right: enable it at RUNTIME over the proven quoted SCPI
path, PROVE the chip honoured it, then measure acoustic case A.

Why a second attempt: the first one wrote at boot and verified with an
UNQUOTED base64 token -- REGister:READ? returns nothing for that form (the
repo's proven path quotes it: REGister:READ? "b64"), so every read was None
and 'automute on' was never actually established. "Automute is not the
reason you get nothing" was right: we got nothing because the instrument
was broken, not because the chip refused.

Second bug found on review: the boot value 0x50 cleared reg2 bits[3:0],
the MIX_SEL map -- CH2_MIX=00 routes channel 1 to DAC2 (mono-L out!). The
correct value keeps the chip's reset map 0b0100 (CH2<2, CH1<1):
  reg2 = 01 MUTE-on-automute | 0 ANALOG_AUTOMUTE (do not yank reg 3)
         | 1 TIMER_FAST | 0b0100 mix map          = 0x54
  reg4 = 128  (~5.3 ms at 48 kHz with TIMER_FAST: 2^21/(128*64*48000))
  reg5 = 104  (-104 dB, chip default, pinned)

Protocol per arm (OFF then ON, same session, same bench):
  write regs (arm ON only; OFF arm restores reset 0x04=0, 0x02=0x34)
  read back via QUOTED path -- arm fails immediately if mismatch
  stream host zeros, poll 0x48: arm ON must ASSERT automute bits (10/20),
     arm OFF must never assert                       (feature-state proof)
  stream 1 kHz tone, poll 0x48: automute must be CLEAR while playing
     in BOTH arms                                 (no mute-forever fix)
  capture E.A.R.S. during zeros: case-A metric 1kHz(zeros) - 1kHz(no-stream)
     gate < +6.0 dB                               (the actual question)

If A passes with automute PROVEN asserted, the case-A tone is the unmuted
modulator's idle artifact and the boot fix is 0x54/128/104. If A still
fails with automute proven asserted on zeros and clear on tone, automute
is genuinely not the mechanism and the mic-side result stands reported.
"""
import base64
import math
import os
import re
import subprocess
import sys
import time
import wave

import numpy as np

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
RATE = 48000
TONE_HZ = 1000.0
SECS = 3
REG_ON = {0x02: 0x54, 0x04: 128, 0x05: 104}
REG_OFF = {0x02: 0x34, 0x04: 0x00}   # chip reset values (read at boot, see log)


def scpi(cmd, timeout=10):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def regs_read(addrs):
    """QUOTED base64 form -- the unquoted form returns nothing (bug that
    voided the first automute attempt)."""
    tok = base64.b64encode(bytes(addrs)).decode()
    out = scpi(f'REGister:READ? "{tok}"').strip().strip('"')
    try:
        return list(base64.b64decode(out, validate=True))
    except Exception:
        return None


def regs_write(d):
    """ONE register per REGister:WRITE call. The firmware decodes
    INTERLEAVED (addr,val) pairs -- the earlier concatenation of all
    addrs then all vals handed the chip (reg2=4, reg5=84, reg0x80=104)
    for what should have been (2,4,5)=(0x54,128,104), corrupting the
    crosstalk-comp register on the way. Multi-pair encoding stays out;
    per-register writes are verified individually by the caller."""
    ok = True
    for a, v in d.items():
        tok = base64.b64encode(bytes([a, v])).decode()
        out = scpi(f'REGister:WRITE "{tok}"')
        ok = ok and ("ERROR" not in out.upper())
    return ok


def card_number(*pats):
    for line in open("/proc/asound/cards"):
        m = re.search(r"^\s*(\d+)\s*\[", line)
        if m and any(p in line.lower() for p in pats):
            return m.group(1)
    return None


def amixer(card, *args):
    return subprocess.run(["amixer", "-q", "-c", str(card)] + list(args),
                          capture_output=True, text=True)


XMOS = os.environ.get("CSD_XMOS_CARD") or card_number("xmos")
MIC = os.environ.get("CSD_MIC_CARD") or card_number("e36db", "e.a.r.s", "36db")
if not XMOS or not MIC:
    sys.exit(f"cards missing (XMOS={XMOS} MIC={MIC})")

print(scpi("*IDN?"))

# Snapshot every register this rig will touch, BEFORE touching anything.
# Includes 0x80 because the previous run's encoding bug wrote it with a
# guessed value; the snapshot captures whatever boot actually left there.
SNAP_ADDRS = [0x02, 0x04, 0x05, 0x80]
_snap = regs_read(SNAP_ADDRS)
if _snap is None or len(_snap) != len(SNAP_ADDRS):
    sys.exit("pre-experiment snapshot failed -- refusing to experiment blind")
SNAP = dict(zip(SNAP_ADDRS, _snap))
print("pre-experiment snapshot:", {f"0x{a:02X}": f"0x{v:02X}"
                                   for a, v in SNAP.items()})

# mic alive (PEAK, not median -- a quiet room legitimately empties median)
for line in subprocess.run(["amixer", "-c", str(MIC), "controls"],
                           capture_output=True, text=True).stdout.splitlines():
    if "Mic Capture Switch" in line and line.startswith("numid="):
        amixer(MIC, "cset", line.split(",", 1)[0].split("=")[1], "--", "on")
for nid in (5, 6):
    amixer(XMOS, "cset", f"numid={nid}", "0,0")
    time.sleep(0.1)
    amixer(XMOS, "cset", f"numid={nid}", "192,192")
scpi("GENerator:OFF")

Z = os.path.join(os.environ.get("TMPDIR", "/tmp"), "amu_zeros.wav")
T = os.path.join(os.environ.get("TMPDIR", "/tmp"), "amu_tone.wav")


def stim(path, tone):
    n = int(RATE * (SECS + 6))
    buf = bytearray()
    for i in range(n):
        v = 0
        if tone:
            v = int(round(0.8 * (2 ** 23 - 1)
                          * math.sin(2 * math.pi * TONE_HZ * i / RATE)))
        u = v & 0xFFFFFF
        word = bytes((u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF))
        buf += word + word
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
        w.writeframes(bytes(buf))


stim(Z, False); stim(T, True)


def play(wav):
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
         "-i", wav, "-f", "alsa", "-ac", "2", f"plughw:{XMOS},0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(p):
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
    time.sleep(0.3)


def cap_dbfs(tag):
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    raw = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"amu_{tag}.raw")
    if os.path.exists(raw):
        os.remove(raw)
    subprocess.run(["arecord", "-q", "-D", f"hw:{MIC},0", "-f", "S24_3LE",
                    "-r", str(RATE), "-c", "2", "-d", str(SECS), "-t", "raw",
                    raw], capture_output=True)
    if not os.path.exists(raw) or os.path.getsize(raw) < 3 * 2048:
        return None
    b = np.frombuffer(open(raw, "rb").read(), dtype=np.uint8)
    b = b[:(b.size // 3) * 3].reshape(-1, 3)
    v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
         (b[:, 2].astype(np.int32) << 16))
    v = np.where(v & 0x800000, v - (1 << 24), v)
    out = {}
    for name, s in (("L", v[0::2]), ("R", v[1::2])):
        x = s.astype(np.float64)
        peak = int(np.max(np.abs(s)))
        if peak < 32:
            print(f"    [{tag} {name}] MIC DEAD (max|s| {peak}) -- run void")
            sys.exit(3)
        spec = np.abs(np.fft.rfft(x - x.mean())) * 2.0 / s.size
        k = int(round(TONE_HZ * s.size / RATE))
        pk = float(np.max(spec[max(1, k - 3):k + 4]))
        out[name] = 20 * math.log10(max(pk, 1e-12) / 2 ** 23)
    return out


def poll_automute(seconds=2.5):
    """(saw_L, saw_R, last0x48) over the window."""
    sl = sr = False
    last = None
    t0 = time.time()
    while time.time() - t0 < seconds:
        r = regs_read([0x48])
        if r is None:
            print("    0x48 readback failed -- instrument broken, abort")
            sys.exit(4)
        last = r[0]
        sl = sl or bool(r[0] & 0x10)
        sr = sr or bool(r[0] & 0x20)
        time.sleep(0.1)
    return sl, sr, last


def arm(name, regs, expect_assert):
    print(f"\n===== arm {name}: write {regs} =====")
    if regs:
        # ONE register per call, each verified immediately. The previous
        # multi-pair call read back [4,0,84] for [0x54,128,104] -- values
        # shifted a slot, which either indicts the firmware's multi-pair
        # write path (real bug, then it gets its own fix) or my encoding.
        # Per-register write/read distinguishes them: same encoding, one
        # pair, proven by reg_write_smoke.
        for a in sorted(regs):
            if not regs_write({a: regs[a]}):
                print(f"  FAIL: write 0x{a:02X} no ack"); return False
            back = regs_read([a])
            if back is None or back[0] != regs[a]:
                print(f"  FAIL: 0x{a:02X} readback {back} != {regs[a]:#x}"
                      " -- arm aborted (multi-pair path is the suspect)")
                return False
            print(f"  0x{a:02X} := {regs[a]:#x} verified")
    else:
        print("  (no writes: reset state, automute disabled)")

    # no-stream baseline
    a0 = cap_dbfs(f"{name}_a0")
    if a0 is None:
        print("  FAIL: no-stream capture failed"); return False

    # zeros
    p = play(Z); time.sleep(1.5)
    if p.poll() is not None:
        print("  ffmpeg died (zeros)"); return False
    sl, sr, st48 = poll_automute()
    az = cap_dbfs(f"{name}_zeros")
    stop(p)
    if az is None:
        print("  FAIL: zeros capture failed"); return False
    print(f"  zeros: 0x48=0x{st48:02X} automute_L={sl} automute_R={sr}"
          f"  (expect assert={expect_assert})")
    if sl != expect_assert or sr != expect_assert:
        print(f"  FAIL: automute feature state wrong on zeros -- arm invalid")
        return False

    # tone: automute must be CLEAR (no mute-forever)
    p = play(T); time.sleep(1.2)
    if p.poll() is not None:
        print("  ffmpeg died (tone)"); return False
    tl, tr, tt = poll_automute()
    at = cap_dbfs(f"{name}_tone")
    stop(p)
    if at is None:
        print("  FAIL: tone capture failed"); return False
    print(f"  tone:  0x48=0x{tt:02X} automute_L={tl} automute_R={tr}"
          f"  (expect clear), tone level {max(at.values()):+.1f} dBFS")
    if tl or tr:
        print("  FAIL: automute stuck during tone")
        return False
    if max(at.values()) < -20.0:
        print("  FAIL: tone not audible -- mic/path suspect")
        return False

    dL = az["L"] - a0["L"]
    dR = az["R"] - a0["R"]
    ok = max(dL, dR) < 6.0
    print(f"  CASE A metric: zeros-vs-A0  L {dL:+.1f}  R {dR:+.1f} dB"
          f"  -> {'PASS' if ok else 'FAIL'} (gate < +6.0)")
    return ok


arm_off = arm("OFF", None, False)
arm_on = arm("ON", REG_ON, True)

# Restore the chip to boot state. Snapshot was taken at rig start, BEFORE
# any write -- hardcoding targets would just trust my datasheet reading;
# the chip's own pre-experiment words are authoritative, including reg 0x80
# which the earlier bad encoding corrupted (boot writes BYPASS_CT|PLL_LOCK
# there; do not guess its value, restore what it was).
print("\nrestore chip to snapshot state:")
restore_ok = True
for a in sorted(SNAP):
    regs_write({a: SNAP[a]})
    back = regs_read([a])
    good = back is not None and back[0] == SNAP[a]
    restore_ok = restore_ok and good
    print(f"  0x{a:02X} -> {SNAP[a]:#x} readback {back}"
          f" {'OK' if good else 'MISMATCH'}")

print(f"\nAUTOMUTE A/B: OFF-arm={'ok' if arm_off else 'fail/invalid'}"
      f"  ON-arm={'ok' if arm_on else 'fail/invalid'}")
if arm_off and arm_on:
    print("RESULT: automute ON fixes case A (both arms valid) -> boot fix"
          " reg2=0x54 reg4=128 reg5=104")
    sys.exit(0)
if arm_off and not arm_on:
    print("RESULT: automute genuinely not the mechanism (ON arm valid, A"
          " still fails) -- report mic-side physics, do NOT ship the fix")
    sys.exit(1)
print("RESULT: inconclusive (an arm's own feature-state gate failed)")
sys.exit(2)
