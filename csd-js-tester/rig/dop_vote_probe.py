#!/usr/bin/env python3
"""Why does the mixer build's DoP vote latch at frame ~27 509?

Facts to discriminate (from rig/dsd_gate_check.py runs):
  production build: latch at ~238 frames; lib_xua dsd_flag 1; witness peak
                    0.244
  mixer build:      latch at ~27 509 frames; sup_dop fires (343 293);
                    witness peak 0.026 (20 dB down)

The 4-vote latch needs 4 CONSECUTIVE raw-word matches, so a 27 509-frame
wait means the match rate AT THE MIXER VOTE POINT is only a few percent --
the words the mixer thread votes on are not the marker-carrying words most
of the time. Candidate causes, to be separated by the probe counters
(GENerator:DOPpRObe? -> frames,matches,lastL,lastR):

  A. the vote runs on every frame but sees non-marker words: matches small,
     frames ~= stream frames.
  B. the vote runs on fewer frames than the stream (rate split above
     96 kHz puts half the exchange elsewhere): frames << stream frames.
  C. the words ARE markers most frames and something resets the streak
     (a non-marker frame every so often): matches/frames high, but the
     latch is still late -> then lastL/lastR on a non-match frame shows
     which byte breaks DOP_MARK.

Decode aid: the rig's payload is
  word = (marker<<24)|(marker<<16)|(payload<<8)|marker, marker 0x05/0xFA
so a HEALTHY word prints lastL=0x05MM0505 style; byte0 (LSB) must equal
byte2. Compare lastL/lastR against that shape.

Run: run_loaded.sh rig/dop_vote_probe.py
"""
import os
import re
import subprocess
import sys
import time
import wave

import numpy as np

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
PY = f"{APP}/.venv/bin/python"
RATE = 176400
FRAMES = RATE * 3           # 3 s of payload
DOP_WAV = "/tmp/dop_vote_dop.wav"
fails = []


def check(cond, what, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {what}"
          + (f"  [{extra}]" if extra else ""))
    if not cond:
        fails.append(what)


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def nums(s):
    return [int(x) for x in re.findall(r"-?\d+", s)]


def card_number(*pats):
    for line in open("/proc/asound/cards"):
        m = re.search(r"^\s*(\d+)\s*\[", line)
        if m and any(p in line.lower() for p in pats):
            return m.group(1)
    return None


XMOS = os.environ.get("CSD_XMOS_CARD") or card_number("xmos")
if not XMOS:
    sys.exit("XMOS card missing")

# payload identical to rig/dsd_gate_check.py's proven builder
marker = np.zeros(FRAMES, dtype=np.uint32)
marker[0::2] = 0x05
marker[1::2] = 0xFA
rng = np.random.default_rng(20261031)
payload8 = rng.integers(0, 256, FRAMES, dtype=np.uint32)
words = (marker << 24) | (marker << 16) | (payload8 << 8) | marker
stereo = np.stack([words, words], axis=1).reshape(-1)   # L0 R0 L1 R1 ...
with wave.open(DOP_WAV, "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(4)
    w.setframerate(RATE)
    w.writeframes(stereo.astype("<u4").tobytes())
# READ IT BACK. Without this, "the mixer never voted" and "the host wrote
# garbage" are the same observation -- the proven gate rig documents exactly
# this trap (a sequential-block interleave passed silently until the
# round-trip caught it).
with wave.open(DOP_WAV, "rb") as w:
    back = np.frombuffer(w.readframes(w.getnframes()), dtype="<u4")
    ch, sw, fr = w.getnchannels(), w.getsampwidth(), w.getframerate()
if not (back.size == 2 * FRAMES and ch == 2 and sw == 4 and fr == RATE):
    sys.exit(f"STIMULUS WRONG: size={back.size} want {2*FRAMES}, "
             f"ch={ch} sw={sw} fr={fr} -- fix the rig, not the board")
if not np.array_equal(back[0::2], back[1::2]):
    sys.exit("STIMULUS WRONG: L != R, lib_xua needs both channels equal")
if not np.array_equal(back[0::2], words):
    sys.exit("STIMULUS WRONG: words did not round-trip")
print(f"payload: {FRAMES} frames @{RATE}, marker 0x05/0xFA, byte0==byte2, "
      f"round-trip verified")

scpi("GENerator:OFF")
print("\n== phase 1: generator OFF -- does the vote fire on clean markers ==")
# Recipe copied from the proven rig/dsd_gate_check.py: pcm_s32le (a first
# attempt wrote pcm_s32_le, ffmpeg's reply was rc=8 "Unknown encoder", and
# with stderr silenced the rig died measuring nothing -- the exact failure
# mode documented twice already in this tree). -stream_loop -1 because
# phases 1+2 outlast the 3 s file.
fferr = "/tmp/dop_vote_ffmpeg.err"
play = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
     "-i", DOP_WAV, "-c:a", "pcm_s32le", "-ac", "2", "-ar", str(RATE),
     "-f", "alsa", f"hw:{XMOS}"],
    stdout=subprocess.DEVNULL, stderr=open(fferr, "wb"))
time.sleep(1.5)
if play.poll() is not None:
    txt = open(fferr, errors="replace").read()[-400:]
    sys.exit(f"ffmpeg died rc={play.poll()} before measuring anything; "
             f"said: {txt!r}")
time.sleep(0.5)
f0 = nums(scpi("GENerator:DOPpRObe?"))
print(f"  probe (gen OFF): frames={f0[0]} matches={f0[1]} "
      f"lastL=0x{f0[2]:08x} lastR=0x{f0[3]:08x}")
time.sleep(1.0)
f1 = nums(scpi("GENerator:DOPpRObe?"))
rate_m = (f1[1] - f0[1]) / max(f1[0] - f0[0], 1)
print(f"  delta over 1 s: frames +{f1[0]-f0[0]}, matches +{f1[1]-f0[1]}, "
      f"match rate {rate_m:.3f}")

print("\n== phase 2: armed -- same words, tone trying to sum ==")
r = scpi(f"GENerator:SINe {RATE},1000000,2130706432")
if r.upper().startswith("ERROR"):
    sys.exit(f"arm rejected: {r!r}")
time.sleep(0.3)
g0 = nums(scpi("GENerator:DOPpRObe?"))
e0 = nums(scpi("GENerator:COUNts?"))[0]
time.sleep(1.5)
g1 = nums(scpi("GENerator:DOPpRObe?"))
e1 = nums(scpi("GENerator:COUNts?"))[0]
rate_a = (g1[1] - g0[1]) / max(g1[0] - g0[0], 1)
print(f"  probe (armed): frames +{g1[0]-g0[0]} matches +{g1[1]-g0[1]} "
      f"rate {rate_a:.3f}  lastL=0x{g1[2]:08x} lastR=0x{g1[3]:08x}")
print(f"  emit +{e1-e0} while armed")
scpi("GENerator:OFF")

print("\n== phase 3: REPRODUCE the failure -- armed BEFORE the stream ==")
# Phases 1-2 latched the vote while un-armed, so they can only measure the
# steady state (and they show it is perfect: rate 1.000, emit 0). The gate
# rig's 27 509-frame burst lives in the OTHER order: tone already playing
# when the DoP stream starts, vote starting from released. Stop the stream,
# let the 8-vote release fire on the following silence, arm, then restart.
play.terminate()
try:
    play.wait(timeout=10)
except subprocess.TimeoutExpired:
    play.kill()
time.sleep(1.0)                       # silence long enough to release
rel = nums(scpi("GENerator:DOPpRObe?"))
r = scpi(f"GENerator:SINe {RATE},1000000,2130706432")
if r.upper().startswith("ERROR"):
    sys.exit(f"arm rejected (phase 3): {r!r}")
fferr3 = "/tmp/dop_vote_ffmpeg3.err"
play3 = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
     "-i", DOP_WAV, "-c:a", "pcm_s32le", "-ac", "2", "-ar", str(RATE),
     "-f", "alsa", f"hw:{XMOS}"],
    stdout=subprocess.DEVNULL, stderr=open(fferr3, "wb"))
# Poll the counters fast: the whole burst is ~150 ms on the mixer build,
# so a 5 s sleep would only ever see the settled state (that is how
# phases 1-2 came out green while the gate rig caught a 150 ms leak).
burst_emit = 0
burst_probes = []
t_end = time.monotonic() + 2.5
while time.monotonic() < t_end:
    e = nums(scpi("GENerator:COUNts?"))
    pb = nums(scpi("GENerator:DOPpRObe?"))
    burst_probes.append((round(time.monotonic() - t_end, 2), e[0], pb))
    burst_emit = e[0]
    if pb[1] > 0 and e[0] >= burst_emit:      # suppressed and emit settled
        pass
    time.sleep(0.15)
scpi("GENerator:OFF")
play3.terminate()
try:
    play3.wait(timeout=10)
except subprocess.TimeoutExpired:
    play3.kill()
print(f"  total emit after restart: {burst_emit}")
for t, e, pb in burst_probes[:12]:
    fr, mt = pb[0], pb[1]
    print(f"  t={t:+.2f}s emit={e:>7} probe frames={fr:>7} matches={mt:>7} "
          f"rate={(mt / fr if fr else 0):.3f} "
          f"lastL=0x{pb[2]:08x}")
# release check: did the vote actually release during the gap?
print(f"  probe right after gap, before restart: frames={rel[0]} "
      f"matches={rel[1]} (latch state not exposed here -- emit burst below "
      f"is the functional read)")
if 0 < burst_emit < 3000:
    print("  burst is startup-bounded like production (238): the mixer "
          "path's window is ~100x wider but structurally the same")
elif burst_emit > 20000:
    print("  REPRODUCED the gate rig's ~27.5k burst; the per-probe match "
          "rate above says whether the mixer saw markers during it")

# expected healthy word shape right now in the stream (frame parity
# continues, so exact marker unknown); just show what the mixer last saw
print("\n== verdict hints ==")
if f1[0] - f0[0] == 0:
    print("  B: vote NEVER ran with the stream live -> hook not reached; "
          "check whether the >96 kHz split bypasses mixer1's host leg")
elif rate_m > 0.9:
    print("  words are clean markers with gen OFF; late latch must come "
          "from streak resets during the ARMED phase (compare rate_a)")
elif rate_m < 0.1:
    print("  A: mixer sees non-marker words even with gen OFF -> the words "
          "reaching mixer1 differ from the words reaching audiohub in the "
          "production build; decode lastL/lastR bytes vs "
          "(marker<<24)|(marker<<16)|(payload<<8)|marker")
else:
    print(f"  partial match rate {rate_m:.2f} with gen OFF: some frames "
          "carry markers, some do not -> rate-split half-exchange suspect")
play.terminate()          # -stream_loop -1 never exits on its own
try:
    play.wait(timeout=10)
except subprocess.TimeoutExpired:
    play.kill()

print(f"\nDOP VOTE PROBE: {'PASS' if not fails else 'FAIL'} "
      f"({len(fails)} failures)")
sys.exit(1 if fails else 0)
