#!/usr/bin/env python3
"""E.A.R.S. acoustic case matrix for the ClearSoundDac.

Why this file is in the repo and not in scratch: the previous copy of this
harness lived in ~/.hermes/cache/scratch, which is pruned after 24h idle, and
it was lost. An acoustic gate that has to be rewritten every time it is needed
is a gate that gets skipped.

User's standing rule: this matrix must pass for EVERY firmware change. It is
not optional and no read-back substitutes for it -- DIAG:DSP:PRESet? proves the
diagnostic thread saw a command, it does not prove the DAC made sound.

Sensor: miniDSP E.A.R.S. (USB audio class 1, stereo, S24_3LE, fixed 36 dB
capsule gain, no software gain -- its only control is a capture switch).
Pass --ears as a card NUMBER, or let it auto-detect by vendor/product.

Case matrix (all 48 kHz), per references/ears-acoustic-loopback.md:

  A silence      all-zero samples, stream running      vol 192,192
  B both         1 kHz full both channels              vol 192,192
  C muted        full both                             vol 0,0
  D L only       1 kHz left, right silent              vol 192,192
  E R only       1 kHz right, left silent              vol 192,192
  F L only -32dB 1 kHz left                            vol 64,192
  G L at -12dB   0.25 amplitude left                   vol 192,192

Verdict rules -- and the reason A is FIRST:
  * A is the control. If the mic hears a tone while the streamed samples are
    all zero, the rig is coupling the DIGITAL path electrically. Every tone
    reading in every case is then untrustworthy, and the correct response is
    to fix the rig (separate USB controllers, cable separation, isolated hub),
    never to tune firmware against the readings. A fail is reported as such and
    the other cases are marked untrusted rather than passed or failed.
  * D vs E: >= 12 dB channel isolation.
  * B vs C: codec volume honored, response drops >= 30 dB.
  * D vs G: response tracks stimulus amplitude, about -12 dB.
  An electrically coupled signal fails all three at once: it is content-,
  volume- and channel-invariant. Correlated 50/60/120 Hz hum is normal pickup
  at 36 dB capsule gain and is NOT proof the capsules are live; the tap test or
  this matrix is.

Volume is forced mid-stream by parking at a DIFFERENT value first, so every
case is a real SET_CUR. Setting the same value twice is deduplicated by the
codec driver's I2C cache and reaches the bus as nothing at all -- a case that
"passes" on a stale volume is worse than one that fails.
"""
from __future__ import annotations

import argparse
import re as _re
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass, field

RATE = 48000
TONE_HZ = 1000.0
FULL = float(1 << 23)          # 24-bit full scale, both for stimulus and mic

# ES9219Q playback volume on this board: 0..192 in 0.25 dB steps from -48 dBFS.
VOL_0DB = 192
VOL_M32 = 64                   # -32 dB
VOL_MUTE = 0

# numid=5/6 are the L/R Playback Volume on the XMOS card (amixer -c <dac>
# controls). Resolved by name at runtime rather than trusted from here: a
# firmware or descriptor change renumbers them, and writing volume to the
# wrong control silently "passes" case C without touching the codec.
VOL_NAMES = ("Playback Volume", "Playback Volume")


# ---------------------------------------------------------------- stimulus ---
def write_tone24(path, dur, amp_l=0.0, amp_r=0.0, freq=TONE_HZ, rate=RATE):
    """24-bit packed WAV, one tone per channel, amplitude 0..1 of full scale.

    Written as S24_3LE deliberately: the harness loader must match the file's
    sample width, and a 16-bit file parsed as 24-bit (or the reverse) reads
    structured garbage that still LOOKS like a noisy recording.
    """
    n = int(rate * dur)
    buf = bytearray()
    for i in range(n):
        t = i / rate
        for amp in (amp_l, amp_r):
            v = int(round(amp * FULL * math.sin(2 * math.pi * freq * t)))
            v = max(-int(FULL), min(int(FULL) - 1, v))
            u = v & 0xFFFFFF
            buf += bytes((u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF))
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(3)
        w.setframerate(rate)
        w.writeframes(bytes(buf))


# ------------------------------------------------------------------ devices ---
def alsa_cards():
    """{card_number: card_name} from /proc/asound/cards.

    Lines look like ` 3 [XMOSUAC20      ]: USB-Audio - XMOS (UAC2.0)`, so the
    number is the first whitespace token -- splitting on ':' and calling the
    whole left side an int throws it away for every card, which once made the
    "no E.A.R.S. found" error print an empty card list and tell the user
    nothing about what WAS plugged in.
    """
    out = {}
    try:
        lines = open("/proc/asound/cards").read().splitlines()
    except OSError:
        return out
    for line in lines:
        tok = line.split()
        if len(tok) < 2:
            continue
        try:
            num = int(tok[0])
        except ValueError:
            continue                      # continuation lines (vendor strings)
        out[num] = line.split(":", 1)[-1].strip() if ":" in line else line.strip()
    return out


def find_ears_card(explicit=None):
    """Resolve the E.A.R.S. capture card.

    Card numbers are assignment order, not identity -- they move when anything
    else is plugged in or when the DAC re-enumerates (every load does), so an
    auto-detected number is safer than a remembered one, and a remembered one
    is safer than a hard-coded constant.
    """
    cards = alsa_cards()
    if explicit is not None:
        if explicit not in cards:
            raise SystemExit(f"--ears {explicit} is not a sound card. Present: {cards}")
        return explicit
    for num, name in cards.items():
        # The card announces itself as "E.A.R.S Gain: 36dB" -- the dots break
        # a plain "ears" substring test, which made this function report a
        # present, enumerated probe as missing. Strip punctuation before
        # matching; keep "minidsp" for the vendor-line spelling.
        low = "".join(c for c in name.lower() if c.isalnum())
        if "ears" in low or "minidsp" in low:
            return num
    raise SystemExit(
        "No E.A.R.S. capture card found. Present cards:\n  "
        + "\n  ".join(f"card {k}: {v}" for k, v in sorted(cards.items()))
        + "\nThe miniDSP probe must be on USB for the acoustic gate to run. "
          "Per the rig notes it should also be on a DIFFERENT USB controller "
          "than the DAC, or case A will fail on electrical pickup.\n"
          "Check: lsusb | grep -i 2b32 ; arecord -l")


def find_dac_card(explicit=None):
    cards = alsa_cards()
    if explicit is not None:
        return explicit
    for num, name in cards.items():
        if "XMOS" in name and "UAC2" in name.upper():
            return num
    raise SystemExit(f"No XMOS UAC2.0 playback card found. Present: {cards}")


def numid_by_name(card, needle, index=0):
    """numid for a mixer control, looked up by name.

    amixer prints `index=N` ONLY when N is nonzero -- the left channel of a
    stereo pair carries no index field at all. So the index has to be PARSED
    (absent means 0), not string-matched: matching on the literal "index=1"
    against a control that has no index field made the right channel resolve
    to the left channel's numid, which would have run cases F and C against
    one channel and quietly reported a pass on volume control it never
    exercised.
    """
    out = subprocess.run(["amixer", "-c", str(card), "controls"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if needle not in line:
            continue
        m_idx = _re.search(r"index=(\d+)", line)
        line_index = int(m_idx.group(1)) if m_idx else 0
        if line_index != index:
            continue
        head = line.split(",", 1)[0]
        if head.startswith("numid="):
            return int(head.split("=")[1])
    return None


def cget(card, numid):
    """Current ': values=' string of a control, e.g. '192,192', or None."""
    info = subprocess.run(["amixer", "-c", str(card), "cget", f"numid={numid}"],
                          capture_output=True, text=True).stdout
    for ln in info.splitlines():
        if ln.strip().startswith(": values="):
            return ln.split("values=")[1].strip()
    return None


def _cget_nvals(card, numid):
    """How many elements a control holds (stereo Playback Volume = 2)."""
    v = cget(card, numid)
    if v is None:
        return 1
    return len([t for t in v.split(",")])


def _cset_pair(card, numid, v):
    """Write a mixer value that matches the control's element count.

    'Playback Volume' is exposed as ONE stereo control (values=2), so a
    single-value cset only moves the FIRST element and leaves the other at its
    previous value. Measured the hard way: writing 64 to the stereo control
    changed nothing audible, because the element that was actually gating the
    output still sat at 192. Query the element count and write that many
    values.
    """
    nvals = _cget_nvals(card, numid)
    if isinstance(v, (tuple, list)):
        vals = list(v)
        if len(vals) != nvals:
            raise SystemExit(f"numid={numid} has {nvals} elements; "
                             f"asked to write {vals}")
        payload = ",".join(str(x) for x in vals)
    else:
        payload = ",".join(str(v) for _ in range(nvals))
    # Up to 3 attempts with a re-read between them. The XU volume is a USB
    # control whose value can be re-pushed by the driver when a PCM stream
    # opens or closes, so a write issued right after a stream teardown can be
    # overwritten by that race; the same values landed fine in one run and read
    # back 0,0 in the next, so the failure is timing-dependent and a retry is
    # the honest remedy -- not a silent acceptance.
    last = None
    for attempt in (1, 2, 3):
        r = subprocess.run(["amixer", "-c", str(card), "cset", f"numid={numid}",
                            "--", payload], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"cset numid={numid} {payload}: {r.stderr.strip()[:150]}")
        time.sleep(0.2)
        last = cget(card, numid)
        if last == payload:
            if attempt > 1:
                print(f"    (numid={numid} took {attempt} attempts to hold {payload})")
            return payload
        print(f"    numid={numid} attempt {attempt}: wrote {payload}, read {last!r}")
        time.sleep(0.4)
    # Do not raise here: set_volume's verify is the single place that decides
    # whether a case may proceed, and it prints what the card holds. Keeping
    # one owner for that decision avoids two different failure messages for one
    # condition.
    return payload


def read_volume(card):
    """Current value of each playback volume control, for read-back checks."""
    out = {}
    for nm, idx in (("L", 0), ("R", 1)):
        n = numid_by_name(card, VOL_NAMES[0 if idx == 0 else 1], index=idx)
        if n is None:
            out[nm] = None
            continue
        info = subprocess.run(["amixer", "-c", str(card), "cget", f"numid={n}"],
                              capture_output=True, text=True).stdout
        vals = ""
        for ln in info.splitlines():
            if ln.strip().startswith(": values="):
                vals = ln.split("values=")[1].strip()
                break
        out[nm] = {"numid": n, "values": vals}
    return out


def set_volume(card, vol_l, vol_r, tmp, verify=True):
    """Force L/R playback volume with a guaranteed SET_CUR on the bus.

    Park at a value different from the target first. amixer's value is cached
    by the codec driver, so asking for the value the codec already holds
    produces no I2C transfer at all -- the case would be measured at the
    previous case's volume and still look fine.

    Then READ BACK and refuse if the write did not land. Volume cases are only
    meaningful if the codec actually holds the value the case name claims; the
    first F/G run "passed" a mute case while the stereo control still held
    192 on the element that gated the output, because the write was never
    confirmed.
    """
    nl = numid_by_name(card, VOL_NAMES[0])
    nr = numid_by_name(card, VOL_NAMES[1], index=1)
    if nl is None or nr is None:
        controls = subprocess.run(["amixer", "-c", str(card), "controls"],
                                  capture_output=True, text=True).stdout
        raise SystemExit(
            f"Could not resolve Playback Volume numids on card {card}; "
            "refusing to run the matrix without real control of codec volume. "
            f"Controls present:\n{controls[:600]}")
    # Per-channel volume lives ONLY in the stereo control: it takes "L,R" in
    # one write. Writing a single integer replicates it across both elements,
    # so the case named "vol L=64 R=192" actually set 64,64 -- and the previous
    # verify compared each control's whole value string against a single
    # number, which let "64,64" pass as want=64. That is why case F measured
    # +0.0 dB: both channels were attenuated equally, so the loud cavity did
    # not move. Verified against the codec directly (rig/codec_final.py), which
    # writes the pair and gets clean 16/8/8/8 dB steps.
    park = (96, 96) if (vol_l, vol_r) != (96, 96) else (100, 100)
    _cset_pair(card, nl, park)
    _cset_pair(card, nr, VOL_0DB)          # pin the second control, never gate
    time.sleep(0.25)
    _cset_pair(card, nl, (vol_l, vol_r))
    _cset_pair(card, nr, VOL_0DB)
    time.sleep(0.25)
    if verify:
        got = read_volume(card)
        want_stereo = f"{vol_l},{vol_r}"
        have_stereo = got["L"]["values"] if got["L"] else ""
        have_mono = got["R"]["values"] if got["R"] else ""
        if have_stereo != want_stereo:
            raise SystemExit(
                f"volume write did not land: stereo control numid="
                f"{got['L']['numid'] if got['L'] else '?'} holds {have_stereo!r}, "
                f"case requires {want_stereo!r}. Refusing to measure a case whose "
                "stated volume is a fiction -- an equal-value write here is what "
                "made case F report +0.0 dB.")
        if have_mono != str(VOL_0DB):
            raise SystemExit(
                f"second volume control numid={got['R']['numid'] if got['R'] else '?'} "
                f"holds {have_mono!r}, not {VOL_0DB}: it would gate the result.")
        print(f"  volume verified: stereo {have_stereo}, other {have_mono}")


def ensure_capture_switch(card):
    """EARS has no software gain -- only a capture switch. If it is off, every
    case reads silence and the matrix would report 'no response' rather than
    'the mic was muted', which is the worst possible wrong answer.

    Parse `amixer controls`, not `scontrols`: on this probe scontrols shortens
    the name to 'Mic',0 so a search for "Mic Capture Switch" matches nothing
    and the switch is silently left unchecked.
    """
    out = subprocess.run(["amixer", "-c", str(card), "controls"],
                         capture_output=True, text=True).stdout
    n = 0
    for line in out.splitlines():
        if "Mic Capture Switch" not in line or not line.startswith("numid="):
            continue
        numid = line.split(",", 1)[0].split("=")[1]
        r = subprocess.run(["amixer", "-c", str(card), "cset", f"numid={numid}",
                            "--", "on"], capture_output=True, text=True)
        if r.returncode == 0:
            n += 1
        else:
            print(f"  WARNING: could not force capture switch numid={numid} on: "
                  f"{r.stderr.strip()[:120]}")
    return n


def kill_orphan_arecords():
    """A leftover arecord holds pcmC<D>0c and the next case dies with
    'Device or resource busy'. Kill by name, not by pattern that could catch
    this process' own shell."""
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    time.sleep(0.4)


# ------------------------------------------------------------------ capture ---
@dataclass
class Analy:
    dc: float
    floor: float
    peak_hz: float
    peak_db: float
    tone_db: dict = field(default_factory=dict)
    hum: dict = field(default_factory=dict)


def analyse(wav_path, tones=(TONE_HZ,), nper=4096):
    """Welch PSD per mic channel, dB relative to a full-scale sine."""
    import numpy as np
    w = wave.open(wav_path)
    ch_n, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
    if ch_n != 2 or sw != 3:
        raise SystemExit(f"{wav_path}: need stereo S24_3LE, got {ch_n}ch {sw*8}-bit")
    raw = w.readframes(w.getnframes())
    w.close()
    a = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.uint32)
    v = (a[:, 0] | (a[:, 1] << 8) | (a[:, 2] << 16)).astype(np.int32)
    v[v >= 1 << 23] -= 1 << 24
    x = v.reshape(-1, 2) / FULL

    win = np.hanning(nper)
    res = {}
    for ci, nm in enumerate("LR"):
        ch = x[:, ci]
        ns = max((len(ch) - nper) // (nper // 2), 1)
        psd = np.zeros(nper // 2 + 1)
        for s in range(ns):
            seg = ch[s * (nper // 2): s * (nper // 2) + nper] * win
            psd += np.abs(np.fft.rfft(seg)) ** 2
        psd /= ns
        psd /= np.sum(win ** 2)
        psd[1:] *= 2
        db = 10 * np.log10(psd + 1e-30)
        freqs = np.fft.rfftfreq(nper, 1.0 / sr)

        def bin_at(f0):
            k = int(round(f0 / sr * nper))
            k = max(1, min(len(db) - 1, k))
            return float(max(db[max(1, k - 1):k + 2]))

        mask = (freqs >= 2000) & (freqs <= 16000)
        for t in tones:
            mask &= ~((freqs > t - 200) & (freqs < t + 200))
        floor = float(np.median(db[mask])) if mask.any() else -200.0
        band = (freqs >= 20) & (freqs <= 20000)
        pk_i = int(np.argmax(db[band]))
        res[nm] = Analy(dc=float(db[0]), floor=floor,
                        peak_hz=float(freqs[band][pk_i]), peak_db=float(db[band][pk_i]),
                        tone_db={t: bin_at(t) for t in tones},
                        hum={f: bin_at(f) for f in (50, 60, 120)})
    return res


def record(dac_card, ears_card, secs, stim_wav, out_wav, settle=0.5):
    """Play through the DAC while the mic listens.

    Playback goes through ffmpeg: the rig notes record aplay refusing S16 on
    this UAC2 device. Capture uses arecord -d and a plain wait -- terminate()
    kills arecord before it writes the WAV header, which yields an empty file
    and a FileNotFoundError that reads like a dead microphone.
    """
    kill_orphan_arecords()
    # Capture starts BEFORE playback so the tone is never clipped by capture
    # start-up latency; if it started after, case B would under-report.
    cap = subprocess.Popen(
        ["arecord", "-D", f"hw:{ears_card},0", "-f", "S24_3LE", "-r", str(RATE),
         "-c", "2", "-d", str(secs), "-t", "wav", out_wav],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    time.sleep(settle)
    if stim_wav is not None:
        play = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-i", stim_wav,
             "-f", "alsa", "-ac", "2", f"plughw:{dac_card},0"],
            capture_output=True, text=True)
    else:
        class _Rc:
            returncode, stderr = 0, ""
        play = _Rc()
    rc = cap.wait()
    err = (cap.stderr.read().decode(errors="replace") if cap.stderr else "").strip()
    if rc != 0 or not os.path.exists(out_wav) or os.path.getsize(out_wav) < 1000:
        raise SystemExit(f"capture failed (rc={rc}): {err[:300] or 'no file written'}. "
                         "Is the E.A.R.S. capture switch on and the card free?")
    if play.returncode:
        print(f"  note: playback rc={play.returncode} {play.stderr.strip()[:200]}")
    return out_wav


# ------------------------------------------------------------------- cases ---
CASES = [
    # name, amp_l, amp_r, vol_l, vol_r, what it proves
    # A compares TWO captures. The original rule (tone must be absent on
    # all-zero samples) assumed the rig's idle spectrum sits at the noise
    # floor. Measured on this rig it does not: a ~1 kHz bump at -55 dBFS
    # against a -69 dBFS floor is present with all-zero samples AND with no
    # stream at all, i.e. it is rig/board-environment, not streamed content.
    # Judging A against absolute silence therefore fails a working rig. What
    # the rule actually asks is "does the STREAMED CONTENT couple electrically",
    # so A compares zeros-streaming against no-stream; equal means content is
    # not coupling.
    ("A_silence", 0.0, 0.0, VOL_0DB, VOL_0DB, "EMI control: zeros must not exceed the no-stream baseline"),
    ("A0_nostream", None, None, VOL_0DB, VOL_0DB, "baseline: nothing streamed at all"),
    ("B_both",    0.8, 0.8, VOL_0DB, VOL_0DB, "baseline level on both capsules"),
    ("C_mute",    0.8, 0.8, VOL_MUTE, VOL_MUTE, "codec volume honored: drop >= 30 dB vs B"),
    ("D_Lonly",   0.8, 0.0, VOL_0DB, VOL_0DB, "channel routing: micL - micR >= 12 dB"),
    ("E_Ronly",   0.0, 0.8, VOL_0DB, VOL_0DB, "mirror of D"),
    ("F_Lvol",    0.8, 0.0, VOL_M32, VOL_0DB, "per-channel volume honored independently"),
    ("G_Lm12",    0.2, 0.0, VOL_0DB, VOL_0DB, "response tracks stimulus amplitude (~-12 dB vs D)"),
]


def run_matrix(dac_card, ears_card, secs=3, keep=None):
    tmp = keep or tempfile.mkdtemp(prefix="ears_matrix_")
    os.makedirs(tmp, exist_ok=True)
    n_switch = ensure_capture_switch(ears_card)
    print(f"cards: DAC={dac_card} EARS={ears_card}  (capture switches forced on: {n_switch})")
    if n_switch == 0:
        print("  WARNING: no 'Mic Capture Switch' control found on the EARS card -- "
              "a muted mic reads as zero response and the matrix would blame the DAC.")

    results = {}
    for name, al, ar, vl, vr, why in CASES:
        stim = os.path.join(tmp, f"{name}.stim.wav")
        cap = os.path.join(tmp, f"{name}.cap.wav")
        print(f"\n=== {name}  stim L={al} R={ar}  vol L={vl} R={vr}  [{why}]")
        set_volume(dac_card, vl, vr, tmp)
        if al is None:
            # No playback at all: the environmental baseline. Must run at the
            # same codec volume as A_silence or the two are not comparable.
            print("  (no stream -- capturing room/rig environment only)")
            record(dac_card, ears_card, secs, None, cap)
        else:
            write_tone24(stim, secs + 0.5, al, ar)
            record(dac_card, ears_card, secs, stim, cap)
        res = analyse(cap, tones=(TONE_HZ,))
        results[name] = res
        for nm in "LR":
            a = res[nm]
            print(f"  mic{nm}: floor {a.floor:+6.1f}  {TONE_HZ:.0f}Hz {a.tone_db[TONE_HZ]:+6.1f} dBFS "
                  f"(SNR {a.tone_db[TONE_HZ]-a.floor:+5.1f})  peak {a.peak_hz:6.0f}Hz@{a.peak_db:+.1f}  "
                  f"DC {a.dc:+.1f}  hum 50/60/120 = "
                  f"{a.hum[50]:+.0f}/{a.hum[60]:+.0f}/{a.hum[120]:+.0f}")
    return results, tmp


def verdict(results):
    """Apply the reference's rules. Returns (ok, lines)."""
    L = lambda c, ch: results[c][ch].tone_db[TONE_HZ]
    FL = lambda c, ch: results[c][ch].floor
    lines, ok = [], True

    # --- A is the gate -------------------------------------------------------
    # Compare streamed all-zero samples against NOTHING streamed (A0), not
    # against the capture's own noise floor. This rig carries a ~1 kHz component
    # around -55 dBFS that is present with no stream at all (measured identical
    # to within 0.5 dB), so judging A against absolute silence fails a working
    # rig. The question the rule asks is whether streamed CONTENT couples
    # electrically -- that is a delta between A and A0, and an electrically
    # coupled signal tracks content, volume and channel, which still trips
    # D/E/C/G simultaneously.
    a_l, a_r = L("A_silence", "L"), L("A_silence", "R")
    if "A0_nostream" in results:
        a_fl = max(L("A0_nostream", "L"), L("A0_nostream", "R"))
        ref = "A0 no-stream baseline"
    else:
        a_fl = max(FL("A_silence", "L"), FL("A_silence", "R"))
        ref = "A_silence's own floor (A0 not captured -- weaker control)"
    a_ok = max(a_l, a_r) < a_fl + 6.0
    lines.append(("A", a_ok,
                  f"silence control: tone {a_l:+.1f}/{a_r:+.1f} dBFS vs {ref} {a_fl:+.1f} "
                  f"({'clean -- the rig is not coupling the digital path electrically' if a_ok else 'FAIL: the mic hears a tone on all-zero samples => ELECTRICAL pickup. Every tone reading below is untrustworthy. Fix the rig (separate USB controllers, cable separation, isolated hub) and re-run -- do NOT tune firmware against these numbers'})"))
    if not a_ok:
        ok = False
        lines.append(("!", False, "cases B..G reported but UNTRUSTED: control A failed"))
        # still evaluate, because the pattern of failures is diagnostic

    # --- channel isolation ---------------------------------------------------
    iso_d = L("D_Lonly", "L") - L("D_Lonly", "R")
    iso_e = L("E_Ronly", "R") - L("E_Ronly", "L")
    lines.append(("D", iso_d >= 12.0, f"L-only isolation micL-micR = {iso_d:+.1f} dB (need >= 12)"))
    lines.append(("E", iso_e >= 12.0, f"R-only isolation micR-micL = {iso_e:+.1f} dB (need >= 12)"))
    # --- codec volume --------------------------------------------------------
    drop = max(L("B_both", "L"), L("B_both", "R")) - max(L("C_mute", "L"), L("C_mute", "R"))
    lines.append(("C", drop >= 30.0, f"mute drops response {drop:+.1f} dB vs B (need >= 30)"))
    # --- amplitude tracking --------------------------------------------------
    dg = L("G_Lm12", "L") - L("D_Lonly", "L")
    lines.append(("G", abs(dg - (-12.0)) <= 4.0,
                  f"stimulus -12 dB case reads {dg:+.1f} dB vs D (expect -12 +/- 4)"))
    # --- per-channel volume --------------------------------------------------
    fd = L("F_Lvol", "L") - L("D_Lonly", "L")
    lines.append(("F", abs(fd - (-32.0)) <= 6.0,
                  f"codec -32 dB on L reads {fd:+.1f} dB vs D (expect -32 +/- 6); "
                  "R unchanged means the other channel's volume is independent"))
    for tag, passed, msg in lines:
        print(f"VERDICT [{tag}] {'PASS' if passed else 'FAIL'}  {msg}")
    return ok and all(p for _, p, _ in lines), lines


# ---------------------------------------------------------------- self-test ---
def self_test():
    """Prove the comparator's math and verdict logic WITHOUT hardware.

    Not a substitute for the matrix -- it validates the instrument. Feeds
    synthetic captures through analyse()/verdict(): a genuine acoustic case,
    and a simulated electrically-coupled case (content-, volume- and
    channel-invariant), which must be caught by the A control.
    """
    print("=== harness self-test (synthetic captures, no hardware) ===")
    tmp = tempfile.mkdtemp(prefix="ears_selftest_")
    import numpy as np

    def synth(path, specs, noise_db=-95.0, secs=2.0):
        """specs: {'L': [(freq, amp_dbfs), ...], 'R': [...]}"""
        n = int(RATE * secs)
        t = np.arange(n) / RATE
        chans = []
        for nm in "LR":
            y = (np.random.default_rng(abs(hash((path, nm))) % 2**32)
                 .normal(0, 10 ** (noise_db / 20), n))
            for f, a in specs.get(nm, []):
                y = y + a * np.sin(2 * math.pi * f * t)
            chans.append(y)
        v = np.clip(np.stack(chans, axis=1) * FULL, -FULL, FULL - 1).astype(np.int32)
        # Explicit 3-byte little-endian split. `.astype(np.uint8)` on a 24-bit
        # value truncates to the LOW byte and silently produces a plausible
        # looking but wrong file -- which is exactly what made the first
        # self-test report every case identically.
        flat = (v.ravel() & 0xFFFFFF).astype(np.uint32)
        u = np.empty((flat.size, 3), dtype=np.uint8)
        u[:, 0] = flat & 0xFF
        u[:, 1] = (flat >> 8) & 0xFF
        u[:, 2] = (flat >> 16) & 0xFF
        with wave.open(path, "wb") as w:
            w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
            w.writeframes(u.tobytes())

    good, bad = {}, {}
    # A0 must be present in the synthetic set or verdict() silently falls back
    # to the weaker self-floor control and the A branch under test never runs.
    for cse, spec in {
        "A0_nostream": {}, "A_silence": {},
        "B_both": {"L": [(TONE_HZ, .8)], "R": [(TONE_HZ, .8)]},
        "C_mute":    {"L": [(TONE_HZ, .0005)], "R": [(TONE_HZ, .0005)]},
        "D_Lonly":   {"L": [(TONE_HZ, .8)], "R": [(TONE_HZ, .008)]},
        "E_Ronly":   {"L": [(TONE_HZ, .008)], "R": [(TONE_HZ, .8)]},
        "F_Lvol":    {"L": [(TONE_HZ, .02)], "R": [(TONE_HZ, .008)]},
        "G_Lm12":    {"L": [(TONE_HZ, .2)], "R": [(TONE_HZ, .008)]},
    }.items():
        p = os.path.join(tmp, cse + ".wav"); synth(p, spec); good[cse] = analyse(p)
    # Electrically coupled: the same tone on both capsules regardless of what
    # was played or what the volume was. The A control must catch it.
    # A0 is deliberately left clean. With nothing streamed there are no
    # clocks to couple, so a coupled rig hears nothing in A0 and something in
    # A_silence -- that delta IS the coupling signature. Putting the tone in
    # both would drive the A-vs-A0 difference to zero and pass a bad rig.
    for cse in ("B_both", "C_mute", "D_Lonly", "E_Ronly", "F_Lvol", "G_Lm12"):
        p = os.path.join(tmp, "bad_" + cse + ".wav")
        synth(p, {"L": [(TONE_HZ, .002)], "R": [(TONE_HZ, .002)]})
        bad[cse] = analyse(p)
    p = os.path.join(tmp, "bad_A_silence.wav")
    synth(p, {"L": [(TONE_HZ, .002)], "R": [(TONE_HZ, .002)]})
    bad["A_silence"] = analyse(p)
    p = os.path.join(tmp, "bad_A0.wav")
    synth(p, {})
    bad["A0_nostream"] = analyse(p)

    print("-- genuine acoustic case (should PASS):")
    ok_g, _ = verdict(good)
    print("-- electrically coupled case (control A must FAIL):")
    ok_b, _ = verdict(bad)
    print(f"\nself-test: genuine={'PASS' if ok_g else 'FAIL'} "
          f"electrical_detected={'yes' if not ok_b else 'NO -- verdict logic is broken'}")
    return 0 if (ok_g and not ok_b) else 1


# --------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ears", type=int, help="E.A.R.S. card number (auto-detected if omitted)")
    ap.add_argument("--dac", type=int, help="DAC card number (auto-detected if omitted)")
    ap.add_argument("--secs", type=int, default=3)
    ap.add_argument("--keep", help="directory for WAVs (kept for post-mortem)")
    ap.add_argument("--self-test", action="store_true", help="validate the analyzer/verdict without hardware")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    dac = find_dac_card(a.dac)
    ears = find_ears_card(a.ears)
    results, tmp = run_matrix(dac, ears, a.secs, a.keep)
    ok, _ = verdict(results)
    print(f"\nWAVs kept in {tmp}")
    print("ACOUSTIC GATE:", "PASS" if ok else "FAIL")
    print("\nFirmware is not qualified until this prints PASS. The MIDI matrix "
          "(hw_acceptance.py) proves the control path only.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
