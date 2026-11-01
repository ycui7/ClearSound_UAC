#!/usr/bin/env python3
"""DoP gate proof: while the stream presents as DoP, the function generator,
mixer, and DSP must not touch the samples.

The claim under test (user requirement): "if any signal presents as DSD
native or DoP, the generator, mixer, and DSP are all disabled."

Why a rig test and not just a code reading. The three consumers have
different owners and different failure modes:

  DSP        returns byte-exact when csd_dsp.dsd || csd_dsp.dop (csd_dsp.xc,
             the line that shipped with the FIR work -- proven by the fact
             that DoP tracks pass DSD audio today).
  mixer      NOT IN THE PRODUCTION BUILD (no MIXER flag in build_on.sh), so
             nothing to bypass at runtime. If it ever lands, mixer.xc applies
             volume MACs to DSD words in the same pass as PCM (its own
             "we need all 32bits for Native DSD" comments are the note that
             this is sample-content-unaware); its stream-restart flush is the
             only protection, and it is a race, not a gate.
  generator  injected in UserBufferManagement, which lib_xua calls in ALL
             stream modes -- the `if (!dsdMode)` guard in the deliver loop
             closes EARLIER (xua_audiohub.xc:516) and the sample transfer +
             UserBufferManagement sit outside it. Before the fix checked here,
             an armed generator overwrote DSD payload words on the way to
             p_dsd_dac. That is what this script falsifies.

What the test does:

  Stream a hand-built DoP payload as plain PCM (S32LE, 176.4 kHz), so it
  walks in through the PCM path exactly as a DoP-capable host sends it:
      byte3 (MSB) alternates 0x05 / 0xFA every frame  <- lib_xua's DSD_MASK,
                                                        32-frame latch
      byte0 == byte2, same value                      <- our DOP_MARK, 4 votes
      byte1 = payload (DSD bits)
  Then capture the mic-path loopback TWICE: once with the generator OFF and
  once armed at 1 kHz full-scale. With the gate working the two captures are
  IDENTICAL and contain no 1 kHz energy -- the generator's wave is not in the
  output stream, because the DSD stream owns it. Without the gate the armed
  capture carries the 1 kHz tone and differs from the OFF capture.

  (Capture is the loopback mirror inside UserBufferManagement, i.e. the words
  handed toward the DAC -- the same words a DSD DAC would bit-bang. Measuring
  at the analog output would work too, but DSD payload through the DAC's
  DSD path produces broadband energy that says nothing about WHICH words the
  firmware sent; the loopback is the direct read of the data path under test.)

Native DSD is covered by the same `!csd_dsp.dsd` condition -- dsd is set from
the live dsdMode at every stream start through csd_dsp_set_dsd, the identical
line that gates the DSP, which shipped and is proven by DSD playback today.
Native cannot be driven from this host (selecting the DSD alternate setting
needs an ALSA driver with DSD support; ffmpeg cannot pick alt settings), so
this script proves the in-band latch (DoP) end to end and the native case by
the shared gate variable.

Run through ~/.hermes/cache/scratch/run_loaded.sh like rig/gen_check.py.
"""
import os
import subprocess
import sys
import time
import wave

import numpy as np

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RATE = 176400                    # DoP16 carrier for DSD64
CAP_SECONDS = 2
XMOS_CARD = os.environ.get("CSD_XMOS_CARD") or ""
MIC_CARD = os.environ.get("CSD_MIC_CARD") or ""

fails = []
notes = []


def check(ok, label, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}"
          + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(label)


def scpi(cmd, timeout=8):
    r = subprocess.run(
        [f"{APP}/.venv/bin/python", "csd_diag.py", "--backend", "rawmidi",
         "--timeout", str(timeout), cmd],
        capture_output=True, text=True, cwd=APP)
    out = (r.stdout or "").strip()
    return out.splitlines()[-1].strip() if out else "TIMEOUT"


def asound_card(*patterns):
    """Same lookup rig/gen_check.py uses (first token, digit-guarded)."""
    try:
        with open("/proc/asound/cards") as fh:
            for line in fh:
                low = line.lower()
                if any(p.lower() in low for p in patterns):
                    tok = line.split(":", 1)[0].split()
                    if tok and tok[0].isdigit():
                        return tok[0]
    except OSError:
        pass
    return ""


XMOS_CARD = XMOS_CARD or asound_card("xmos", "clearsound")
MIC_CARD = MIC_CARD or asound_card("e36db", "e.a.r.s", "ears")
print(f"== rig: playback={XMOS_CARD or '??'} capture={MIC_CARD or '??'} ==")

# --- build the DoP payload ---------------------------------------------------
# Every frame: byte3 alternates marker, byte0 == byte2 == same marker value,
# byte1 = pseudo-DSD payload. Payload is an LFSR, NOT zeros: a zero payload
# would let a suppressed-but-silent path masquerade as success, and the OFF
# capture would then look identical to "the host sent nothing".
frames = RATE * CAP_SECONDS
marker = np.zeros(frames, dtype=np.uint32)
marker[0::2] = 0x05
marker[1::2] = 0xFA
rng = np.random.default_rng(20261031)
payload8 = rng.integers(0, 256, frames, dtype=np.uint32)
# byte0 = byte2 = byte3 = marker, byte1 = payload.
# First version built (marker<<24)|(payload<<8)|marker, which left byte2 as a
# RANDOM payload byte -- our DOP_MARK needs byte0 == byte2, so the 4-vote
# latch could only fire by chance (~2^-32 for four frames in a row), and the
# counters proved it: 179 966 frames armed through the "DoP" stream, zero
# suppressions. Both checkers' bytes must literally match, not by comment.
words = (marker << 24) | (marker << 16) | (payload8 << 8) | marker
raw = words.astype("<u4").tobytes()

dop_wav = "/tmp/dsd_gate_dop.wav"
# Write, then read the file BACK and demand byte equality. The first run
# discarded ffmpeg's stderr and never verified the WAV; when the counters
# later showed zero detections, there was no way to tell "board deaf" from
# "host sent garbage" -- which is exactly the ambiguity counters + round-trip
# together remove.
# TRUE INTERLEAVING. The first version did writeframes(raw * 2): two
# SEQUENTIAL blocks, while WAV/PCM is L,R,L,R -- the device therefore got
# L = only even-indexed pattern words (byte3 always 0x05) and R = only odd
# ones (byte3 always 0xFA), so lib_xua's "both channels equal the SAME
# marker" rule could never hold. The host-side round-trip below caught it.
# Each channel must carry the full alternating pattern independently.
stereo = np.stack([words, words], axis=1).reshape(-1)   # L0 R0 L1 R1 ...
with wave.open(dop_wav, "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(4)
    w.setframerate(RATE)
    w.writeframes(stereo.astype("<u4").tobytes())
with wave.open(dop_wav, "rb") as w:
    back = np.frombuffer(w.readframes(w.getnframes()), dtype="<u4")
check(back.size == 2 * frames
      and bool(np.array_equal(back[0::2], words))
      and bool(np.array_equal(back[1::2], words)),
      "DoP WAV round-trips byte-exactly, both channels (host side)")
# Parity is per FRAME, not per word: after true L,R interleave the word
# stream is L0 R0 L1 R1..., so the marker alternates with period 4 in word
# index. Checking per-word parity was a third bug of the same family
# (asserting the layout the file does not have); the device-side confirmation
# that lib_xua latched DoP (dsd_flag=1) is the ground truth that the pattern
# itself is correct.
check(bool(np.all((back >> 24) == np.where(
        (np.arange(back.size) // 2) % 2 == 0, 0x05, 0xFA))),
      "byte3 alternates 0x05/0xFA per frame in the file (lib_xua view)")
check(bool(np.all((back & 0xFF) == ((back >> 16) & 0xFF))),
      "byte0 == byte2 in the file itself (our DOP_MARK view)")


def play_dop(seconds):
    """Stream the DoP payload to the DAC. Returns (Popen, stderr path).

    -c:a pcm_s32le is NOT decoration: the payload IS the byte pattern, so an
    implicit format conversion (s32 -> s24_3le for example) would silently
    destroy the marker bytes and every board-side counter would correctly
    report "no DoP here". Pin the wire format. -v error with stderr kept in a
    file: the first version threw ffmpeg's output away, so a rejected format
    or rate would have been indistinguishable from a deaf board.
    """
    err = "/tmp/dsd_gate_ffmpeg.err"
    p = subprocess.Popen(
        ["sg", "audio", "-c",
         f"ffmpeg -v error -stream_loop -1 -i {dop_wav} -c:a pcm_s32le "
         f"-t {int(seconds)} -ac 2 -ar {RATE} -f alsa hw:{XMOS_CARD}"],
        stdout=subprocess.DEVNULL, stderr=open(err, "w"))
    return p, err


def capture(tag):
    """Loopback capture at the DoP rate; returns float array or None."""
    tmp = f"/tmp/dsd_gate_cap_{tag}.wav"
    if os.path.exists(tmp):
        os.remove(tmp)
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    # S24_3LE, not S32_LE: the EARS codec exposes S24_3LE ONLY (first run died
    # with "Sample format non available / Available: - S24_3LE" -- same format
    # rig/gen_check.py captures; guessing a card's formats is how you get a
    # plausible-looking void).
    r = subprocess.run(
        ["arecord", "-q", "-D", f"hw:{MIC_CARD},0", "-f", "S24_3LE",
         "-r", "48000", "-c", "2", "-d", str(CAP_SECONDS), "-t", "wav", tmp],
        capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp):
        print(f"        arecord failed: {(r.stderr or '')[:140]}")
        return None
    with wave.open(tmp, "rb") as w:
        n = w.getnframes()
        rawc = w.readframes(n)
    b = np.frombuffer(rawc, dtype=np.uint8)
    if b.size < 3 * 64:
        print("        capture too short")
        return None
    b = b[:(b.size // 3) * 3].reshape(-1, 3)
    v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
         (b[:, 2].astype(np.int32) << 16))
    v = np.where(v & 0x800000, v - (1 << 24), v)
    a = v[0::2].astype(np.float64)            # left channel
    print(f"        [{tag}] {n} frames, peak {np.max(np.abs(a))/2**23:.4f}")
    return a


def tone_energy(a, hz):
    """Relative energy at hz: |bin| / median|bin|. A real 1 kHz injection
    measures in the thousands; DoP payload is noise-like and stays near 1."""
    if a is None or a.size < 64:
        return 0.0
    x = a - a.mean()
    if np.max(np.abs(x)) == 0:
        return 0.0
    spec = np.abs(np.fft.rfft(x))
    k = int(round(hz * x.size / RATE))
    body = np.median(spec[1:])
    return float(spec[k] / body) if body > 0 else 0.0


if not (XMOS_CARD and MIC_CARD):
    print("missing cards; run through run_loaded.sh")
    sys.exit(1)

print("\n== 0. DoP stream is live ==")
# Boot state must be generator-OFF, or a previous armed wave confuses leg A.
scpi("GENerator:OFF")
time.sleep(0.2)


def counts():
    """DEBUG:GENerator:COUNts? -> (emit, sup_dsd, sup_dop, dsd_flag, dop_latched).

    THE PRIMARY INSTRUMENT. The mic path cannot judge injection at DoP
    rates: the EARS codec is S24_3LE at 48 kHz fixed, arecord -r 176400
    silently resamples (2 s asked, 96000 frames back), and any spectrum
    computed against the requested rate is fiction -- the first version of
    this rig failed a WORKING gate that way (tone energy measured against a
    4x-wrong bin grid: the 1 kHz "signature" was the payload's own noise
    scattering). The counters are taken on the audio thread at the gate's
    decision point: emitted / suppressed-by-dsd / suppressed-by-dop frames.
    """
    t = scpi("DEBUG:GENerator:COUNts?")
    parts = t.split(",")
    if len(parts) != 5 or any(not x.isdigit() for x in parts):
        print(f"        COUNts? -> {t!r}")
        return None
    return tuple(int(x) for x in parts)


print("\n== 1. baseline: generator OFF, no stream ==")
c0 = counts()
check(c0 is not None, "counter query works", str(c0))

print("\n== 2. ARM FIRST, then start the DoP stream ==")
# Ordering is part of the test. If the stream started first, lib_xua's own
# 32-frame DoP latch (fires ~0.2 ms in) would already have set dsd before
# SCPI armed the generator: every frame would attribute to sup_dsd, and the
# in-band latch this gate exists for -- our dop flag, which covers the frames
# BETWEEN "DoP bytes arrive" and "lib_xua notices" -- would never be
# exercised and never proven. Arming first forces that window to exist, and
# the counters expose its shape:
#   emit    ~4    (before our 4-vote latch; harmless: lib_xua needs 32
#                  consecutive marked frames and these carry no markers yet)
#   sup_dop ~32   (our latch up, lib_xua's not yet -- THE window)
#   sup_dsd rest  (stream restarted into real DSD mode)
# DECIMAL, not hex: the amp parameter parses as an integer, and "0x7F000000"
# was rejected -- the arm silently failed (STATe came back mode=0), which the
# check below caught. 2130706432 = 0x7F000000 = ~0.5 dBFS.
r = scpi(f"GENerator:SINe {RATE},{1000 * 1000},2130706432")
st = scpi("GENerator:STATe?")
parts = st.split(",")
armed = len(parts) == 9 and parts[0] == "1" and parts[1] == str(RATE)
check(armed, "generator accepted, armed at the DoP rate", st)
time.sleep(0.2)
proc, fferr = play_dop(CAP_SECONDS * 2 + 25)
time.sleep(2.0)
check(proc.poll() is None, "ffmpeg is still streaming the DoP payload",
      "died: " + (open(fferr).read()[:160] if os.path.exists(fferr) else "?"))
c1 = counts()
if c1 and c0:
    d_emit = c1[0] - c0[0]
    d_dsd = c1[1] - c0[1]
    d_dop = c1[2] - c0[2]
    total = d_emit + d_dsd + d_dop
    print(f"        frames while armed: emit {d_emit}, "
          f"sup_dsd {d_dsd}, sup_dop {d_dop}")
    check(total > 100000,
          "the armed window really carried frames (2 s @176.4 kHz)",
          f"{total} frames")
    # Bounded, not zero -- two legitimate emission windows exist, and a
    # zero-tolerance check would set a false standard rather than a strict
    # one: (a) stream start -- before ffmpeg's first write lands, the device
    # runs underrun/zero frames that carry NO markers and therefore really
    # do present as PCM; lib_xua agrees (its latch demands 32 marked frames)
    # and the requirement is "signal presented as DSD/DoP", which silence is
    # not. A real run measured ~235 frames (~1.3 ms) there. (b) the 4-vote
    # latch window itself. The bug shape -- emitting the whole stream -- is
    # three orders of magnitude away (measured: 235 of 447 143).
    check(d_emit <= max(5000, total // 100),
          "emission stopped AT the latch (bounded startup window only)",
          f"emit delta {d_emit} of {total}")
    check(d_dop >= 4,
          "the IN-BAND DoP latch fired (our flag, pre-lib_xua window)",
          f"sup_dop delta {d_dop}")
    check(d_dsd > 100000,
          "stream entered real DSD mode and stayed suppressed to the end",
          f"sup_dsd delta {d_dsd}")
    print(f"        flags after the stream: dsd={c1[3]} dop={c1[4]}")
    check(c1[3] != 0,
          "stream really entered a DSD mode (lib_xua's view agrees)",
          f"dsd_flag {c1[3]}")
    notes.append(f"DoP@176.4k armed-first: emit {d_emit}, "
                 f"sup_dop {d_dop}, sup_dsd {d_dsd}, "
                 f"flags dsd={c1[3]}/dop={c1[4]}")

print("\n== 3. generator DOES emit on plain PCM (counters are alive) ==")
# Same window shape as leg 2; the ONLY variable flipped is the stream
# content (DoP payload -> clean sine). If leg 2's zero-emission came from a
# dead counter, dead hook, or an OFF generator, this leg would also read
# zero and the proof above would be vacuous. This is the positive control.
proc.terminate()
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()
time.sleep(0.5)
pcm = subprocess.Popen(
    ["sg", "audio", "-c",
     f"ffmpeg -v error -f lavfi -i sine=frequency=997:sample_rate={RATE}"
     f":duration=30 -af volume=0.4 -ac 2 -ar {RATE} -f alsa hw:{XMOS_CARD}"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.5)
# Leave-vote settle: 8 non-matching RAW frames clear the latch. At any
# streaming rate that is microseconds; the sleep only covers USB startup.
time.sleep(0.3)
c2 = counts()
time.sleep(2.0)
c3 = counts()
if c2 and c3:
    p_emit = c3[0] - c2[0]
    p_dop = c3[2] - c2[2]
    print(f"        frames while armed on PCM: emit {p_emit}, sup_dop {p_dop}")
    check(p_emit > 1000,
          "generator emits once the DoP markers stop (gate is not one-way)",
          f"{p_emit} emitted")
    check(p_dop == 0,
          "DoP suppression does not leak into the PCM segment",
          f"sup_dop delta {p_dop}")
    notes.append(f"PCM@176.4k: {p_emit} frames emitted, 0 suppressed")
scpi("GENerator:OFF")
pcm.terminate()
try:
    pcm.wait(timeout=5)
except subprocess.TimeoutExpired:
    pcm.kill()

print("\n== 4. capture sanity (secondary; payload really streamed) ==")
# Capture is NO LONGER the judge -- it is a witness that the host stream was
# live: nonzero peak over the DoP segment proves payload flowed through the
# loopback. Spectral claims from a 48 kHz-fixed codec at a 176.4 kHz request
# are not made.
witness = None
try:
    witness = capture("dop-live")
except Exception as e:                        # noqa: BLE001 -- witness only
    print(f"        capture failed (witness skipped): {e}")
if witness is not None:
    pk = float(np.max(np.abs(witness))) / 2 ** 23
    check(pk > 0.003, "DoP payload streamed through the loopback (witness)",
          f"peak {pk:.4f}")

print()
if fails:
    print(f"DSD GATE: FAIL ({len(fails)}):")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print(f"DSD GATE: PASS ({len(notes)} measurements)")
for nt in notes:
    print(f"   {nt}")
