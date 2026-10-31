#!/usr/bin/env python3
"""Function-generator rig check: arm each mode, verify the device ADOPTED it,
and measure the wave the DAC actually produced.

Three layers, each falsifiable on its own, because "I can hear a tone" hides
several distinct failures:

  1. COMMAND   -- does the SCPI pattern match and do the parameters parse?
                  Verified on the host by build_native_scpi.sh against real
                  libscpi; re-checked here because the device build and the
                  host build can disagree about the parser configuration.
  2. ADOPTION  -- did the AUDIO thread pick the request up? GENerator:STATe?
                  returns cmd and adopted separately for exactly this. cmd
                  advancing while adopted does not means no stream is running:
                  the generator is fine and the measurement is void. Collapsing
                  the two turns "no stream" into "broken generator", the most
                  expensive kind of misdiagnosis available here.
  3. ACOUSTICS -- is the wave real? Capture the mic path (the proven-good
                  capture route the acoustic gate already uses), check the
                  frequency absolutely, and check amplitude DIFFERENTIALLY.

Must run inside a loaded-firmware lifetime, i.e. through
~/.hermes/cache/scratch/run_loaded.sh, which keeps xrun alive for the duration
and refuses to measure stale firmware. Run against a board whose xrun has
exited, every query times out and section 1 stops the script -- for the right
reason, but the wrong one to debug.

Read-only apart from the generator itself: no register writes, no OTP, no
preset changes.
"""
import os
import subprocess
import sys
import time
import wave

import numpy as np

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Frequency in milliHz (the SCPI unit): 1 Hz = 1000.
HZ = 1000

# Amplitude in Q31 (the device's unit). -6 dBFS = 0x40000000.
AMP_M6DBFS = 0x40000000

RATE = 48000
CAPTURE_SECONDS = 1.0
TEST_HZ = 1000.0

MODE_NAMES = {0: "OFF", 1: "SINE", 2: "SQUARE", 3: "SWEEP", 4: "MULTITONE",
              5: "NOISE"}


def scpi(cmd, timeout=8):
    """One command, reply text. Quoting stays inside argv -- no shell layer,
    which is what mangled the base64 payload in the ad-hoc version."""
    r = subprocess.run(
        [f"{APP}/.venv/bin/python", "csd_diag.py", "--backend", "rawmidi",
         "--timeout", str(timeout), cmd],
        capture_output=True, text=True, cwd=APP)
    out = (r.stdout or "").strip()
    if r.returncode and not out:
        return f"ERROR {(r.stderr or '').strip()[:120]}"
    return out.splitlines()[-1].strip() if out else "TIMEOUT"


fails = []
notes = []


def check(ok, label, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}"
          + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(label)


def asound_card(*patterns):
    """First sound-card index whose /proc/asound/cards line matches a pattern.

    Same lookup the acoustic gate uses, so the two scripts cannot disagree about
    which device is which.

    The index is the first WHITESPACE token, NOT the text before the first
    colon: those lines read `3 [E36dB          ]: USB-Audio - ...`, and that
    bracket contains no colon, so a split on ':' returns "3 [E36dB          ]"
    and arecord then dies with "Cannot get card index for 3 [E36dB          ]".
    The second line of each pair also matches the pattern and its first token is
    not a digit, hence the isdigit() guard -- without it the description line
    wins and hands back "E.A.R.S".
    """
    try:
        with open("/proc/asound/cards") as fh:
            for line in fh:
                low = line.lower()
                if any(pat.lower() in low for pat in patterns):
                    tok = line.split(":", 1)[0].split()
                    if tok and tok[0].isdigit():
                        return tok[0]
    except OSError:
        pass
    return ""


class Stream:
    """Hold a USB audio stream open against the DAC.

    Without a live stream the frame hook never executes, so every command is
    accepted, `cmd` advances, `adopted` does not, and the generator is correct
    while the test reports failure. The firmware's own wording for this hazard
    is why the distinction is reported rather than assumed: "cmd != adopted
    means the stream is not running".

    Feeds SILENCE. That is deliberate: once the generator owns the frame the
    DAC-bound samples are the generator's and the host source is discarded, so a
    silent source removes any chance of attributing host audio to the generator
    -- which is exactly the confusion section 9 exists to rule out.
    """

    def __init__(self, card, seconds):
        self.p = None
        if not card:
            self.err = "XMOS card not found in /proc/asound/cards"
            return
        self.err = ""
        self.p = subprocess.Popen(
            ["sg", "audio", "-c",
             f"ffmpeg -v error -f lavfi -i anullsrc=r={RATE}:cl=stereo "
             f"-t {int(seconds)} -f alsa hw:{card}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.p.kill()


def state():
    """GENerator:STATe? -> dict, or None. Nine integers; the last two are the
    command counter and the audio thread's adoption counter."""
    t = scpi("GENerator:STATe?")
    parts = t.split(",")
    if len(parts) != 9 or any(not p.lstrip("-").isdigit() for p in parts):
        print(f"        STATe? returned {t!r}")
        return None
    keys = ("mode", "fs", "fa_q8", "fb_q8", "amp", "domain", "tones", "cmd",
            "adopted")
    return dict(zip(keys, (int(p) for p in parts)))


def arm_and_adopt(cmd, wait_s=1.5):
    """Send an arm command and wait for the AUDIO thread to adopt it.

    Waits on `adopted` rather than sleeping a fixed time: that counter advances
    only in UserBufferManagement, so adopted == cmd is positive evidence a frame
    boundary occurred after the command. A fixed sleep passes on a dead stream;
    this does not.
    """
    before = state()
    if before is None:
        return None
    reply = scpi(cmd)
    if reply.startswith("ERROR"):
        print(f"        {cmd!r} -> {reply}")
        return None
    after = state()
    if after is None:
        return None
    deadline = time.time() + wait_s
    while after["adopted"] != after["cmd"] and time.time() < deadline:
        time.sleep(0.05)
        after = state()
        if after is None:
            return None
    if after["adopted"] != after["cmd"]:
        print(f"        NOT ADOPTED: cmd={after['cmd']} "
              f"adopted={after['adopted']} -- no stream is running, so the "
              f"frame hook never executed; nothing measured downstream is real")
    return after


def capture_wav(mic_card):
    """One arecord capture from the mic card, left channel, or None.

    arecord, not csd_diag.py: the diag client speaks the MIDI class only and has
    no audio capture. The format is the acoustic gate's (S24_3LE @ 48 kHz
    stereo) so the two measurements are comparable; guessing a format here would
    yield a plausible-looking spectrum of the wrong bytes.
    """
    if not mic_card:
        return None
    tmp = "/tmp/gen_capture.wav"
    if os.path.exists(tmp):
        os.remove(tmp)
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.3)
    r = subprocess.run(
        ["arecord", "-q", "-D", f"hw:{mic_card},0", "-f", "S24_3LE",
         "-r", str(RATE), "-c", "2", "-d", str(int(CAPTURE_SECONDS)),
         "-t", "wav", tmp],
        capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp):
        print(f"        arecord failed: {(r.stderr or '')[:140]}")
        return None
    with wave.open(tmp, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
        ch = w.getnchannels()
    b = np.frombuffer(raw, dtype=np.uint8)
    if b.size < 3 * 64 or n == 0:
        print("        capture too short")
        return None
    # S24_3LE: three bytes little-endian per sample, sign-extended.
    b = b[:(b.size // 3) * 3].reshape(-1, 3)
    v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
         (b[:, 2].astype(np.int32) << 16))
    v = np.where(v & 0x800000, v - (1 << 24), v)
    return (v[0::ch] if ch > 1 else v), n


def analyse(samples, fs):
    """(dominant frequency, peak dBFS against 2^23, spectral tonality).

    Whole-capture real FFT for the peak, then refinement by direct projection
    over the surrounding bin.

    The first version scanned frequency by projecting onto a fresh basis at every
    half-bin -- O(n^2): 144 000 samples x 144 000 candidate frequencies, which
    never finished inside the rig's timeout. An FFT is O(n log n) for the same
    answer; the refinement stays because a bin centre is quantised to fs/n and
    the reported frequency is checked to 1%.

    No window. A rectangular window is right for locating a peak, and a Hamming
    window would bias the reported amplitude -- and amplitude is half the point.

    Tonality (dominant bin over the median of the rest) is reported because
    "is there a tone in here" is a question absolute level cannot answer. Note
    what it does NOT catch: mains hum through the 36 dB preamp measured 253x and
    650x on silent captures -- hum is itself tonal -- so frequency is what
    rejects it, and the noise-floor margin in section 9 is what rejects a
    genuine tone buried under that hum.
    """
    x = np.asarray(samples, dtype=np.float64)
    if x.size < 64:
        return 0.0, -300.0, 0.0
    x = x - x.mean()
    peak = float(np.max(np.abs(x)))
    if peak == 0.0:
        return 0.0, -300.0, 0.0

    n = x.size
    spec = np.abs(np.fft.rfft(x))
    k = int(np.argmax(spec[1:])) + 1            # skip DC (already removed)
    coarse = k * fs / n
    body = np.median(spec[1:]) if spec.size > 2 else 0.0
    tonality = float(spec[k] / body) if body > 0 else float("inf")

    binw = fs / n
    a, b = max(1.0, coarse - binw), coarse + binw
    best_f, best_m = coarse, -1.0
    for f in np.linspace(a, b, 41):
        m = abs(np.dot(x, np.exp(-2j * np.pi * f * np.arange(n) / fs)))
        if m > best_m:
            best_m, best_f = m, f
    return best_f, 20 * np.log10(peak / float(1 << 23)), tonality


def measure_capture(tag, mic_card):
    cap = capture_wav(mic_card)
    if cap is None:
        return None
    left, n = cap
    f_peak, dbfs, tonality = analyse(left, RATE)
    print(f"        [{tag}] {f_peak:.2f} Hz, {dbfs:.2f} dBFS, "
          f"tonality {tonality:.0f}x, {n} frames")
    return f_peak, dbfs, tonality


# ---------------------------------------------------------------------------
XMOS_CARD = os.environ.get("CSD_XMOS_CARD") or asound_card("xmos")
MIC_CARD = os.environ.get("CSD_MIC_CARD") or asound_card("e36db", "e.a.r.s",
                                                          "ears")
print(f"== 0. rig: XMOS playback card={XMOS_CARD or '??'}  "
      f"mic capture card={MIC_CARD or '??'} ==")

stream = Stream(XMOS_CARD, seconds=300)
if stream.err:
    print(f"  cannot start a stream: {stream.err}")
else:
    # ffmpeg needs time to open the endpoint; until it does, the audiohub loop
    # stays idle and `adopted` never advances. The acoustic gate sleeps 3 s
    # after enumeration for the same reason.
    time.sleep(2.0)

print()
print("== 1. state query is well formed ==")
s0 = state()
check(s0 is not None, "GENerator:STATe? parses as 9 integers")
if s0 is None:
    print("  no SCPI reply -- is xrun alive? run this through run_loaded.sh")
    stream.stop()
    sys.exit(1)
print(f"        boot state: mode={MODE_NAMES.get(s0['mode'], s0['mode'])} "
      f"fs={s0['fs']} cmd={s0['cmd']} adopted={s0['adopted']}")
# The generator must be inert until asked, and nothing may be parked: a boot
# state with a pending command means the handshake wedged at reset.
check(s0["mode"] == 0, "boots OFF (audio must be untouched)", f"mode={s0['mode']}")
check(s0["cmd"] == s0["adopted"], "cmd == adopted at rest (nothing pending)")

print("\n== 2. sine: arm, adopt, and read back ==")
s = arm_and_adopt(f"GENerator:SINe 48000,{1000 * HZ},{AMP_M6DBFS}")
if s:
    check(s["mode"] == 1, "mode is SINE", MODE_NAMES.get(s["mode"], "?"))
    check(s["fs"] == 48000, "sample rate took effect", str(s["fs"]))
    check(s["fa_q8"] == 1000 * 256, "1 kHz stored exactly",
          f"fa_q8={s['fa_q8']} want {1000 * 256}")
    check(s["amp"] == AMP_M6DBFS, "amplitude stored", str(s["amp"]))
    check(s["adopted"] == s["cmd"], "audio thread adopted the request",
          f"cmd={s['cmd']} adopted={s['adopted']}")

print("\n== 3. Q8 quantisation is honest ==")
# Frequency is stored in 1/256 Hz steps, so a request that is not a multiple of
# 1/256 Hz must come back as what the device WILL play, not as what was asked
# for. Echoing the request would let a systematic frequency error hide until
# someone measured the output.
s = arm_and_adopt(f"GENerator:SINe 48000,{1000 * HZ + 1},{AMP_M6DBFS}")
if s:
    want = round((1000 * HZ + 1) * 256 / 1000)
    check(abs(s["fa_q8"] - want) <= 1,
          "1000.001 Hz read back as the quantised value",
          f"fa_q8={s['fa_q8']} want~{want}")

print("\n== 4. sweep directions: rising, falling, steady ==")
# All three are legal and come from one closed form,
# f(n) = f_start * (f_stop/f_start)^(n/N): the ratio's exponent carries the
# direction, so there is no direction flag for a caller or implementation to
# get wrong. Endpoints and midpoint are verified numerically against the closed
# form in tools/dsp/test_gen.cpp (host gate, 1346 checks); here the readback is
# checked, which is the part that can only fail on the device.
for label, fa, fb in (("rising", 1 * HZ, 20000 * HZ),
                      ("falling", 20000 * HZ, 1 * HZ),
                      ("steady", 5000 * HZ, 5000 * HZ)):
    s = arm_and_adopt(f"GENerator:SWEep 48000,{fa},{fb},LOG,{AMP_M6DBFS},1000")
    if s:
        check(s["mode"] == 3, f"{label}: mode is SWEEP")
        check(s["domain"] == 1, f"{label}: domain is LOG", str(s["domain"]))
        check(s["fa_q8"] == round(fa * 256 / 1000), f"{label}: f_start",
              str(s["fa_q8"]))
        check(s["fb_q8"] == round(fb * 256 / 1000), f"{label}: f_stop",
              str(s["fb_q8"]))

print("\n== 5. multitone 2..4 ==")
for n, freqs in ((2, (260, 1040)), (3, (260, 1040, 4160)),
                 (4, (260, 1040, 4160, 8000))):
    args = ",".join(str(int(f * HZ)) for f in freqs)
    s = arm_and_adopt(f"GENerator:TONEs 48000,{AMP_M6DBFS},0,{n},{args}")
    if s:
        check(s["mode"] == 4 and s["tones"] == n, f"{n} tones armed",
              f"mode={s['mode']} tones={s['tones']}")

print("\n== 6. noise ==")
s = arm_and_adopt(f"GENerator:NOISe 48000,{AMP_M6DBFS},1000")
if s:
    check(s["mode"] == 5, "noise armed")

print("\n== 7. rejections are refused AND reported ==")
# Two independent criteria per case: an error must be reported, AND the
# generator must not have moved. An implementation that armed the wave and then
# answered an error would pass the first test and be wrong.
bad = [
    ("sub-1 Hz", "GENerator:SINe 48000,999,%d" % AMP_M6DBFS),
    ("above 20 kHz", "GENerator:SINe 48000,20001000,%d" % AMP_M6DBFS),
    ("amp zero", "GENerator:SINe 48000,1000000,0"),
    ("amp overflow", "GENerator:SINe 48000,1000000,4294967295"),
    ("bad sample rate", "GENerator:SINe 12345,1000000,%d" % AMP_M6DBFS),
    ("sweep without period",
     "GENerator:SWEep 48000,1000,20000000,LOG,%d,0" % AMP_M6DBFS),
    ("bad domain",
     "GENerator:SWEep 48000,1000,20000000,NOPE,%d,1000" % AMP_M6DBFS),
    ("1 tone", "GENerator:TONEs 48000,%d,0,1,1000000" % AMP_M6DBFS),
    ("5 tones", "GENerator:TONEs 48000,%d,0,5,1000,2000,3000,4000,5000"
     % AMP_M6DBFS),
]
for label, cmd in bad:
    before = state()
    scpi("SYSTem:ERRor:COUNt?")                       # drain the queue
    scpi(cmd)
    err = scpi("SYSTem:ERRor:NEXT?")
    after = state()
    if after is None or before is None:
        check(False, f"rejection state readable: {label}")
        continue
    moved = (after["mode"] != before["mode"] or after["cmd"] != before["cmd"])
    reported = not err.startswith("0,")
    check(reported and not moved, f"rejected: {label}",
          f"err={err!r} moved={moved}")

print("\n== 8. OFF stops the wave ==")
s = arm_and_adopt("GENerator:OFF")
if s:
    check(s["mode"] == 0, "OFF adopted")
    check(s["adopted"] == s["cmd"], "OFF adopted by the audio thread")

print("\n== 9. acoustic proof ==")
# True analog loopback: generator -> DSP -> DAC -> line cable -> E.A.R.S preamp
# -> its ADC, on the mic path the acoustic gate already trusts. With
# CSD_DSP_DIAG_LOOP the firmware mirrors the DAC-bound frame onto that path, so
# this measures the real output with no new firmware and no new wiring.
#
# FREQUENCY is asserted absolutely: 1 kHz in, 1 kHz out. That is the check that
# catches a sample rate which never reached the oscillator -- such a generator
# is armed, adopted, reads back the frequency it was told, and plays something
# else. Nothing else in this file can catch it.
#
# AMPLITUDE is asserted DIFFERENTIALLY, across USB levels. The chain's gain is
# unknown and is not unity, so asserting an absolute dBFS once failed a correct
# generator at -12.57 dBFS while USB read -6.02. The gain is a CONSTANT, so
# measuring at two levels cancels it: if the generator scales its samples
# correctly, the measured DELTA equals the commanded DELTA whatever the chain
# does in between. One instrument, one subject, one flipped variable.
#
# Two rejected alternatives, both of which produced false failures:
#   - a host-driven reference tone. Two captures of the SAME host tone came back
#     at -19 and -35 dBFS, both clean 1 kHz, so the reference was unreliable and
#     averaging it made a correct generator look 8 dB low.
#   - a test level at USB -24 dBFS. It measured -28.4 dBFS, i.e. quieter than a
#     SILENT capture of the same chain (-25.6 dBFS): the tone was under the
#     capture noise floor, so the reading was the room, not the generator.
# Three USB amplitudes. The table is MEASURED on this bench (chain sits 6.6 dB
# down: USB -6.02 read -12.66 and -12.68 on two captures), not derived:
#
#   Q31         USB dBFS   predicted measured   vs floor -25.6 + 10 dB
#   0x30000000     -8.52        -15.1            clears by 0.5 dB
#   0x40000000     -6.02        -12.6            clears by 3.0 dB
#   0x7F000000     -0.07         -6.7            clears by 8.9 dB
#
# The bottom level is as low as this bench allows. Below it a tone measures
# quieter than a SILENT capture of the same chain, so the reading is the room and
# not the generator -- an earlier level at USB -24 dBFS read -28.4 dBFS against a
# -25.6 dBFS floor and failed repeatability by 1.8 dB, a preamp fault charged to
# the generator. Anything that fails the floor test is REFUSED below, never
# averaged in.
#
# The top is 0x7F000000 rather than 0x7FFFFFFF because at exact full scale the
# Q31 product lands within one LSB of INT32_MAX -- a linearity test should not be
# the thing that discovers a saturation bug.
LEVELS = [0x30000000, 0x40000000, 0x7F000000]
FLOOR_MARGIN_DB = 10.0        # keeps noise <~0.05 dB from the tone's own peak

check(bool(MIC_CARD) and bool(XMOS_CARD), "both cards found",
      f"xmos={XMOS_CARD} mic={MIC_CARD}")

if MIC_CARD and XMOS_CARD:
    scpi("GENerator:OFF")
    time.sleep(0.2)

    # Floor first, with the stream down: the WORST (loudest) silent capture is
    # the floor, because the quiet ones understate it and a level judged against
    # an understated floor is silently contaminated -- the exact failure the
    # -24 dBFS level above fell into.
    stream.stop()
    time.sleep(0.4)
    silent = [m for m in (measure_capture("silent", MIC_CARD) for _ in range(3))
              if m]
    check(bool(silent), "capture noise floor measured")
    floor = max(m[1] for m in silent) if silent else -80.0
    usable_above = floor + FLOOR_MARGIN_DB
    print(f"        floor {floor:.2f} dBFS; amplitude measurable only above "
          f"{usable_above:.2f} dBFS")

    per_level = {}
    for q31 in LEVELS:
        reps = []
        attempt = 0
        while len(reps) < 2 and attempt < 4:
            attempt += 1
            st = Stream(XMOS_CARD, seconds=40)
            time.sleep(1.0)
            s = arm_and_adopt(f"GENerator:SINe 48000,{int(TEST_HZ * HZ)},{q31}")
            tag = f"L{q31:#x}#{attempt}"
            m = (measure_capture(tag, MIC_CARD)
                 if (s and s["adopted"] == s["cmd"]) else None)
            scpi("GENerator:OFF")
            st.stop()
            if m is None:
                pass
            elif abs(m[0] - TEST_HZ) / TEST_HZ > 0.02:
                print(f"        [{tag}] peak {m[0]:.1f} Hz, not the tone")
            elif m[1] <= usable_above:
                print(f"        [{tag}] {m[1]:.2f} dBFS is within "
                      f"{FLOOR_MARGIN_DB:.0f} dB of the floor -- amplitude "
                      f"not measurable at this level")
            else:
                reps.append(m)
            time.sleep(0.3)
        per_level[q31] = reps

    got = {hex(k): len(v) for k, v in per_level.items()}
    usable = {k: v for k, v in per_level.items() if len(v) == 2}
    print(f"        usable levels: {got}")
    check(len(usable) >= 2,
          "at least two levels measurable above the capture floor",
          f"{len(usable)} of {len(LEVELS)}; floor {floor:.2f} dBFS -- if this "
          f"fails the capture chain, not the generator, is the limit")

    for q31, reps in per_level.items():
        if not reps:
            continue
        db = 20 * np.log10(q31 / 2 ** 31)
        for r in reps:
            check(abs(r[0] - TEST_HZ) / TEST_HZ < 0.01,
                  f"generator tone is 1 kHz at USB {db:.2f} dBFS",
                  f"{r[0]:.2f} Hz")
        if len(reps) == 2:
            check(abs(reps[0][1] - reps[1][1]) < 0.5,
                  f"repeatable at USB {db:.2f} dBFS",
                  f"{reps[0][1]:.2f} vs {reps[1][1]:.2f} dBFS")

    if len(usable) >= 2:
        keys = sorted(usable)
        for lo, hi in zip(keys, keys[1:]):
            meas_lo = float(np.mean([r[1] for r in usable[lo]]))
            meas_hi = float(np.mean([r[1] for r in usable[hi]]))
            cmd_delta = 20 * np.log10(hi / lo)
            meas_delta = meas_hi - meas_lo
            print(f"        USB {20*np.log10(lo/2**31):.2f} -> {meas_lo:.2f} "
                  f"dBFS;  USB {20*np.log10(hi/2**31):.2f} -> {meas_hi:.2f} "
                  f"dBFS;  delta {meas_delta:.2f} dB (commanded "
                  f"{cmd_delta:.2f})")
            check(abs(meas_delta - cmd_delta) < 0.5,
                  f"amplitude scales 1:1 across "
                  f"{hex(lo)}->{hex(hi)} (chain gain cancels)",
                  f"measured {meas_delta:.2f} vs commanded {cmd_delta:.2f} dB")
            # The absolute offset belongs to DAC+cable+preamp, so it is printed
            # rather than asserted -- but it is printed at BOTH levels. Two
            # levels agreeing on one offset is the evidence that the delta above
            # measured a linear chain and not two unrelated numbers that
            # happened to differ by the right amount.
            off_lo = meas_lo - 20 * np.log10(lo / 2 ** 31)
            off_hi = meas_hi - 20 * np.log10(hi / 2 ** 31)
            check(abs(off_hi - off_lo) < 0.5,
                  "capture-chain offset is level-independent (chain is linear)",
                  f"{off_lo:.2f} vs {off_hi:.2f} dB")
            notes.append(f"1 kHz: USB {20*np.log10(lo/2**31):.2f}/"
                         f"{20*np.log10(hi/2**31):.2f} dBFS measured "
                         f"{meas_lo:.2f}/{meas_hi:.2f} dBFS, chain "
                         f"~{off_hi:.2f} dB down")

scpi("GENerator:OFF")
stream.stop()
print()
if fails:
    print(f"GEN RIG: FAIL ({len(fails)}):")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print(f"GEN RIG: PASS ({len(notes)} measurements)")
for nt in notes:
    print(f"   {nt}")
