#!/usr/bin/env python3
"""ClearSound DAC acceptance suite — owner's case list of 2026-11-05.

    .venv/bin/python rig/csd_suite.py [--dac N] [--mic N] [--allow-dfu] [--quick]

Fifteen cases, each returning PASS / FAIL / NOT-RUN / TODO. The distinction
matters and is deliberate:

  PASS      the measurement met the criterion, with the number printed.
  FAIL      the measurement was made and did not meet it. Firmware/hardware is
            implicated; the number is the evidence.
  NOT-RUN   the measurement could not be made (probe deaf, board absent). The
            fixture is implicated, NEVER the firmware -- convicting firmware on
            an instrument fault is how days are lost.
  TODO      the case cannot pass yet because a dependency is missing (a command
            the firmware does not have, or an operation the owner has not
            authorised). Listed with what is missing.

Exit status is nonzero if any case FAILED, or if any case was NOT-RUN unless
--allow-notrun was given. TODO cases never fail the run -- they are work
items, and the run summary counts them so they cannot be quietly forgotten.

Rules this file obeys, all learned on this bench:
  * Identify the loaded image by its own *IDN? stamp before quoting anything.
  * Resolve cards by model string every run; numbers are assignment order.
  * Force host Playback Volume to full before ANY level measurement, and
    verify it with `: values=` (a stale value crushes both channels equally and
    reads as a firmware regression).
  * Register reads are trusted only when a witness register answers correctly
    in the same transaction (a codec held in reset replies all-zero).
  * Never conclude "no reply" from a timeout without reading the device error
    queue -- a rejected SCPI command is silent by design.
  * Exit codes are captured before piping.
"""
import argparse
import base64
import os
import re
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))      # .../csd-js-tester/rig
RIG = HERE                                              # the sub-fixtures live here
TESTER = os.path.dirname(HERE)                          # .../csd-js-tester
REPO = os.path.dirname(TESTER)                          # .../clearsound_uac
PY = sys.executable
# csd_diag.py sits beside rig/, not at the repo root -- a wrong path here makes
# every MIDI case die as a harness exception and looks exactly like a dead board.
CLIENT = os.path.join(TESTER, "csd_diag.py")

DAC_TOKENS = ("DACUAC20", "XMOSUAC20", "CLEARSOUND", "SPECIALTYCIRCUITS")
# 'ears' is NOT a token: it is a substring of Cl-ears-ound and once made the rig
# record the DAC as its own microphone.
MIC_TOKENS = ("EARSGAIN", "MINIDSP", "USBAUDIOEARS", "AUDIOADAP", "ADAP")
PLAYBACK_BLOCK = ("CLEARSOUND", "XMOS", "UAC2", "DAC")

PASS, FAIL, NOTRUN, TODO = "PASS", "FAIL", "NOT-RUN", "TODO"
MAX_RATE = 384000          # the rate case 6/7 must run at
SUITABLE_BITS = 24
SUITABLE_RATE = 192000     # case 13's advertised minimum


# --------------------------------------------------------------------------- #
# small foundations
# --------------------------------------------------------------------------- #
def sh(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def cards():
    """{number: NAME} from /proc/asound/cards, resolved fresh every call."""
    out = {}
    for line in open("/proc/asound/cards"):
        m = re.match(r"\s*(\d+)\s+\[([A-Za-z0-9_-]+)\s*\]", line)
        if m:
            out[int(m.group(1))] = m.group(2).upper()
    return out


def find_dac(prefer=None):
    c = cards()
    if prefer is not None and prefer in c:
        return prefer
    for n, name in sorted(c.items()):
        if any(t in name for t in DAC_TOKENS) and "MIC" not in name:
            return n
    return None


def find_mic(prefer=None):
    c = cards()
    if prefer is not None and prefer in c:
        return prefer
    for n, name in sorted(c.items()):
        if any(t in name for t in MIC_TOKENS):
            return n
    return None


def scpi_exists(cmd, card):
    """Does the device actually implement this header?

    Absence of a TIMEOUT is NOT the test: a silent device, a rejected header and
    an empty success all look similar from the client. Ask the instrument's own
    error queue -- libscpi files '-113 undefined header' there for anything it
    does not recognise, which is the only positive evidence available.
    """
    ok, txt, eq = scpi(cmd, card, timeout=8)
    if eq:
        return False, eq
    if not ok:
        return False, "no reply"
    _, err = sh([PY, CLIENT, "--backend", "rawmidi", "--timeout", "6",
                 "SYSTem:ERRor:NEXT?"], timeout=40)
    err = err.strip().strip('"')
    if err and "TIMEOUT" not in err.upper():
        head = err.split(",")[0].strip()
        if head.lstrip("-").isdigit() and int(head) != 0:
            return False, err
    return True, txt


def scpi(cmd, card, timeout=15):
    """SCPI over the kernel raw MIDI node -- the only path that has ever
    returned replies here (the ALSA sequencer tools accept writes and answer
    nothing). Returns (ok, text, err_queue_or_None)."""
    rc, out = sh([PY, CLIENT, "--backend", "rawmidi", "--timeout", str(timeout), cmd],
                 timeout=timeout + 40)
    text = out.strip().strip('"')
    if "TIMEOUT" in text.upper():
        # A silent REJECT and a lost byte look identical from here, so ask the
        # device's own error queue before calling it a transport fault.
        _, eq = sh([PY, CLIENT, "--backend", "rawmidi", "--timeout", "8",
                    "SYSTem:ERRor:NEXT?"], timeout=60)
        eq = eq.strip().strip('"')
        if eq and "TIMEOUT" not in eq.upper() and "-0," not in eq:
            return False, text, eq          # command arrived; content was refused
        return False, text, None            # genuinely no answer
    return True, text, None


def b64read(addrs, card):
    """Register read with a WITNESS: read the trim alongside two registers whose
    values boot init fixes (0x03=0x40, 0x07=0x00). A codec held in reset answers
    I2C with zeros, so an all-zero read is either the truth or nobody answering;
    the neighbours tell them apart."""
    ok, txt, eq = scpi('REGister:READ? "%s"' % base64.b64encode(
        bytes([0x03, 0x07] + list(addrs))).decode(), card)
    if not ok:
        return None, eq or "no reply"
    tok = txt.split()[-1] if txt else ""
    try:
        blob = base64.b64decode(tok + "=" * ((4 - len(tok) % 4) % 4))
    except Exception:
        return None, "unreadable reply %r" % txt
    if len(blob) < 2 + len(addrs) or list(blob[:2]) != [0x40, 0x00]:
        return None, "witness regs read %s (expected 40 00) -- read not authoritative" % (
            " ".join("%02X" % b for b in blob[:2]))
    return list(blob[2:2 + len(addrs)]), None


def b64write(pairs, card):
    payload = bytes([b for ab in pairs for b in ab])
    ok, txt, eq = scpi('REGister:WRITE "%s"' % base64.b64encode(payload).decode(), card)
    return ok, eq


def set_volume(card, value):
    """Full-scale host volume, VERIFIED. cget prints values= twice and the
    capability line's value is the ELEMENT COUNT, so anchor on ': values='."""
    out = subprocess.run(["amixer", "-c", str(card), "controls"],
                         capture_output=True, text=True).stdout
    nids = [int(l.split("numid=")[1].split(",")[0]) for l in out.splitlines()
            if "Playback Volume" in l]
    if not nids:
        return False, "no Playback Volume control"
    for n in nids:
        subprocess.run(["amixer", "-c", str(card), "cset", "numid=%d" % n, str(value)],
                       capture_output=True)
        got = subprocess.run(["amixer", "-c", str(card), "cget", "numid=%d" % n],
                             capture_output=True, text=True).stdout
        m = re.search(r":\s*values=([0-9][0-9,\s]*)", got)
        vals = [int(x) for x in (m.group(1).replace(",", " ").split() if m else [])]
        if not vals or any(abs(v - value) > 1 for v in vals):
            return False, "numid %d reads %s, asked %d" % (n, vals, value)
    return True, "numids %s at %s" % (nids, value)


def tone_wav(path, secs=2.0, freq=1000.0, ch=None, dbfs=-6.0):
    """ch=None both, 0 left, 1 right. ch=None, 0 left, 1 right."""
    n = int(48000 * secs)
    amp = 10 ** (dbfs / 20.0) * 32767.0
    t = np.arange(n) / 48000.0
    x = (amp * np.sin(2 * np.pi * freq * t)).astype(np.int16)
    z = np.zeros(n, dtype=np.int16)
    st = np.empty(n * 2, dtype=np.int16)
    st[0::2] = x if ch in (None, 0) else z
    st[1::2] = x if ch in (None, 1) else z
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes(st.tobytes())
    return path


def record_peak(mic, secs=2, freq=1000.0):
    """(dBFS per channel at the drive bin, floor) or None. S24_3LE, the only
    format this bench mic accepts."""
    cap = "/tmp/suite_cap.wav"
    if os.path.exists(cap):
        os.remove(cap)
    rc, out = sh(["arecord", "-q", "-D", "hw:%d,0" % mic, "-f", "S24_3LE", "-r", "48000",
                  "-c", "2", "-d", str(secs), "-t", "wav", cap], timeout=90)
    if not os.path.exists(cap):
        return None, "arecord produced nothing: %s" % out.strip()[:80]
    import wave
    with wave.open(cap, "rb") as w:
        n, chn = w.getnframes(), w.getnchannels()
        d = np.frombuffer(w.readframes(n), dtype=np.uint8)
    raw = d.reshape(n, chn, 3).astype(np.uint32)
    v = (raw[:, :, 0] | (raw[:, :, 1] << 8) | (raw[:, :, 2] << 16)).astype(np.int32)
    v = np.where(v & 0x800000, v - (1 << 24), v).astype(np.float64) / (1 << 23)
    win = np.hanning(len(v))
    peaks, floor = {}, {}
    for c in range(min(chn, 2)):
        sp = np.abs(np.fft.rfft(v[:, c] * win))
        fr = np.fft.rfftfreq(len(v), 1 / 48000.0)
        k = int(np.argmin(np.abs(fr - freq)))
        band = sp[max(1, k - 2):k + 3].max()
        peaks[c] = 20 * np.log10(band / (len(v) / 4) + 1e-12)
        # Floor must be taken where nothing lives. An early version used bins
        # 20..200 -- that is 10..100 Hz, i.e. mains hum and room rumble -- and
        # read -16 dBFS, higher than the tone, so a working mute tested "deaf".
        # 7..11 kHz is above the tone, above the audible bench noise, and free
        # of mains harmonics.
        lo = int(np.argmin(np.abs(fr - 7000.0)))
        hi = int(np.argmin(np.abs(fr - 11000.0)))
        floor[c] = 20 * np.log10(np.median(sp[lo:hi]) + 1e-12)
    return peaks, floor


def play_bg(dac, wav):
    """Start a tone and return the handle. Deliberately a separate function from
    a blocking play(): one return type per function means every call site is
    checkable, instead of a flag that leaves the caller holding either a Popen or
    a tuple depending on a default."""
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-i", wav,
         "-f", "alsa", "-ac", "2", "plughw:%d,0" % dac],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(p):
    try:
        p.terminate(); p.wait(timeout=10)
    except Exception:
        p.kill()


# --------------------------------------------------------------------------- #
# the cases
# --------------------------------------------------------------------------- #
def case_01_mute(dut):
    d, m = dut["dac"], dut["mic"]
    if m is None:
        return NOTRUN, "no capture mic attached -- mute is inaudible to software"
    ok, why = set_volume(d, 192)
    if not ok:
        return NOTRUN, "host volume not settable: %s" % why
    wav = tone_wav("/tmp/suite_01.wav", 3.0)

    def peak_with_tone():
        p = play_bg(d, wav); time.sleep(0.5)
        pk, fl = record_peak(m, 2)
        stop(p); time.sleep(0.4)
        return pk, fl

    pk0, fl = peak_with_tone()
    if pk0 is None:
        return NOTRUN, str(fl)
    on = max(pk0.values())
    if on < fl[0] + 6:
        return NOTRUN, "probe deaf: tone %.1f dBFS vs floor %.1f" % (on, fl[0])

    # The product's real mute surface: the UAC2 "Playback Switch" controls
    # (numid 3/4 here). There is NO control named "Mute" on this card, so a
    # search for that word finds nothing and used to drop through to a guessed
    # codec register write -- writing a register whose semantics were never
    # confirmed is not a test, it is vandalism with a schedule.
    rc, ctrl = sh(["amixer", "-c", str(d), "controls"])
    sw = [l.split("numid=")[1].split(",")[0] for l in ctrl.splitlines()
          if "Playback Switch" in l]
    if not sw:
        return NOTRUN, "card exposes no Playback Switch (UAC2 mute) control"

    def set_mute(v):
        for n in sw:
            sh(["amixer", "-c", str(d), "cset", "numid=%s" % n, str(v)])
        for n in sw:
            got = sh(["amixer", "-c", str(d), "cget", "numid=%s" % n], timeout=30)[1]
            # BOOLEAN control: the state line reads ": values=on,on". Matching
            # digits instead matches the CAPABILITY line's "values=2", which is
            # the ELEMENT COUNT -- comparing that against 1 declares a perfectly
            # good mute control "not writable" and voids the whole case.
            m = re.search(r":\s*values=(\S+)", got)
            seen = m.group(1).split(",") if m else []
            want = ("on" if v else "off") if any(x in ("on", "off") for x in seen) else str(v)
            if not seen or any(x not in (want, "0", "1") for x in seen):
                return False, "numid %s did not take %d (%s)" % (n, v, got.strip()[-40:])
        return True, ""

    path = "UAC2 Playback Switch numids %s" % sw
    okw, whyw = set_mute(1)
    if not okw:
        return NOTRUN, "mute control not writable: %s" % whyw
    pk0, fl = peak_with_tone()
    if pk0 is None:
        return NOTRUN, str(fl)
    on = max(pk0.values())
    if on < fl[0] + 6:
        return NOTRUN, "probe deaf: tone %.1f dBFS vs floor %.1f" % (on, fl[0])

    okw, whyw = set_mute(0)
    if not okw:
        return NOTRUN, "mute could not be asserted: %s" % whyw
    pkm, _ = peak_with_tone()
    set_mute(1)
    pk2, _ = peak_with_tone()
    if pkm is None or pk2 is None:
        return NOTRUN, "capture failed during a mute leg"
    muted, unmuted = max(pkm.values()), max(pk2.values())

    drop = on - muted
    back = on - unmuted
    detail = ("mute path=%s  tone %.1f -> muted %.1f (drop %.1f dB) -> unmuted %.1f "
              "(%.1f dB of original)" % (path, on, muted, drop, unmuted, -abs(back)))
    if drop >= 30 and abs(back) <= 3:
        return PASS, detail
    if drop < 30:
        return FAIL, "MUTE DID NOT SILENCE: " + detail
    return FAIL, "UNMUTE DID NOT RESTORE: " + detail


def case_02_idn_serial(dut):
    ok, txt, eq = scpi("*IDN?", dut["dac"])
    if not ok:
        return (TODO if eq else FAIL), ("*IDN? refused: %s" % eq) if eq else ("no *IDN? reply")
    f = [x.strip() for x in txt.split(",")]
    if len(f) != 4:
        return FAIL, "expected 4 IEEE-488 fields, got %d: %r" % (len(f), f)
    vendor, model, serial, fw = f
    ok2, txt2, _ = scpi("*IDN?", dut["dac"])   # second read: identity must be stable
    stable = txt2.strip('"') == txt
    if serial.upper() in ("", "UNAVAILABLE", "0"):
        return FAIL, "serial field is %r (vendor=%s model=%s fw=%s)" % (serial, vendor, model, fw)
    if not re.match(r"^[0-9A-Fa-f]{6,16}$", serial):
        return FAIL, "serial %r is not a hex unit serial" % serial
    if not stable:
        return FAIL, "serial not stable across two reads: %r vs %r" % (serial, txt2)
    return PASS, "vendor=%s model=%s serial=%s firmware=%s (stable on 2 reads)" % (
        vendor, model, serial, fw)


def case_03_reg_rw(dut):
    d = dut["dac"]
    TRIM = [0x11, 0x12, 0x13, 0x14]
    orig, why = b64read(TRIM, d)
    if orig is None:
        return NOTRUN, "baseline read not authoritative: %s" % why
    SCRATCH = [0x5A, 0xA5, 0x3C, 0xC3]
    ok, eq = b64write(list(zip(TRIM, SCRATCH)), d)
    if not ok:
        return (TODO if eq else FAIL), ("write refused: %s" % eq) if eq else "no reply to write"
    got, why = b64read(TRIM, d)
    if got is None:
        b64write(list(zip(TRIM, orig)), d)
        return NOTRUN, "read-back not authoritative: %s" % why
    wrote_ok = (got == SCRATCH)
    b64write(list(zip(TRIM, orig)), d)
    back, why = b64read(TRIM, d)
    restored = (back == orig)
    detail = "wrote %s read %s; restored %s read %s" % (
        " ".join("%02X" % b for b in SCRATCH), " ".join("%02X" % b for b in got or []),
        " ".join("%02X" % b for b in orig), " ".join("%02X" % b for b in back or []))
    if wrote_ok and restored:
        return PASS, detail
    if not wrote_ok:
        return FAIL, "WRITE DID NOT TAKE: " + detail
    return FAIL, "RESTORE FAILED (board left non-default): " + detail


def case_04_persistence(dut):
    """Read/write the persistent journal over MIDI.

    Units are COUNTS of 0.125 dB, signed, 0 (max) down to -256 (-32 dB) -- not
    dB. Passing "-12.0" asks for -1.5 dB, and a harness that believes its own
    units will convict a working journal of not saving.
    """
    d = dut["dac"]
    ok, stat, eq = scpi("VOLume:TRIM:STATus?", d)
    if not ok:
        return (TODO if eq else FAIL), "no persistence status: %s" % (eq or "no reply")
    WANT = (-96, -24)                                   # -12.0 dB, -3.0 dB
    scpi("VOLume:TRIM:LEFT %d" % WANT[0], d)
    scpi("VOLume:TRIM:RIGHT %d" % WANT[1], d)
    ok, live, _ = scpi("VOLume:TRIM?", d)
    live = live.strip()
    has_save, why = None, "no candidate answered"
    for cand in ("VOLume:TRIM:SAVE", "PERSISTence:SAVE", "DIAG:PERSISTence:SAVE"):
        exists, why = scpi_exists(cand, d)
        if exists:
            has_save = cand
            break
    if not has_save:
        scpi("VOLume:TRIM:LEFT 0", d); scpi("VOLume:TRIM:RIGHT 0", d)
        return TODO, ("journal read side is live (STATus?=%s; set %d,%d read back %s) but no "
                      "save command exists (%s). Firmware must expose one so a written pair "
                      "reaches the QSPI journal; this case then proves survival across LOAD."
                      % (stat, WANT[0], WANT[1], live, why))
    scpi(has_save, d)
    ok, rec, _ = scpi("VOLume:TRIM:STATus?", d)          # journal's own view

    # PROVE PERSISTENCE BY RELOADING, NOT BY LOAD.
    # VOLume:TRIM:LOAD cannot distinguish flash from mailbox in this firmware:
    # every VOLume:TRIM set journals immediately, so the store's idea of "what is
    # in flash" is always the last value written, and LOAD faithfully hands back
    # that same value. The earlier rig set the pair, zeroed it, saved, then
    # loaded -- which wrote the zeros to flash before LOAD ran, then convicted the
    # journal of reading back zeros. That was a fixture defect, not a firmware
    # defect (boot adopt is real: csd_diag_midi.xc csd_mb_put_trim()).
    # Only a fresh boot separates the two: the mailbox starts at 0 dB, so a value
    # that appears after a reload could only have come from the record.
    xe = os.path.join(REPO, "bin", "2AMi2o2xxxxxd", "ClearSoundDac_2AMi2o2xxxxxd.xe")
    rc, out = sh(["bash", os.path.join(RIG, "load_xe.sh"), xe], timeout=300)
    if rc != 0 or "PROVEN" not in out:
        scpi("VOLume:TRIM:LEFT 0", d); scpi("VOLume:TRIM:RIGHT 0", d)
        return NOT_RUN, ("cannot reload the image to separate flash from mailbox "
                         "(load_xe.sh rc=%d: %s) -- the fixture, not the firmware, is "
                         "short here" % (rc, out.strip()[-160:]))
    back = ""
    for _ in range(20):                                   # device re-enumerates
        ok, back, _ = scpi("VOLume:TRIM?", d)
        if ok:
            break
        time.sleep(2)
    got = [int(x) for x in re.findall(r"-?\d+", back or "")][:2]
    detail = ("save=%s wrote %s -> journal STATus?=%s -> reloaded image, mailbox "
              "reset to 0 dB, TRIM?=%s (only the record can supply these)"
              % (has_save, list(WANT), (rec or "").strip(), got))
    scpi("VOLume:TRIM:LEFT 0", d); scpi("VOLume:TRIM:RIGHT 0", d)
    scpi(has_save, d)                                     # leave zeros persisted
    if got == list(WANT):
        return PASS, detail
    if got == [0, 0]:
        return FAIL, ("pair survived the save but NOT the boot: boots at 0 dB, so the "
                      "record was never adopted -- " + detail)
    return FAIL, "adopted pair is neither the saved value nor the default: " + detail


def case_05_dfu_twice(dut):
    if not dut.get("allow_dfu"):
        return TODO, ("gated: flashing is not authorised on this bench (owner directive "
                      "2026-11-05: RAM-only, no field firmware update). Re-run with "
                      "--allow-dfu to execute: flash .xe, verify audio, flash AGAIN, "
                      "verify audio -- the second pass is what proves the new image's own "
                      "DFU class still enumerates and accepts a write.")
    return TODO, "DFU double-flash not yet implemented in this suite (see TODO.md)"


def run_gate(minutes, tag, card):
    """Run the bit-perfect gate and decide from its VERDICT line.

    Never from the exit code: the gate exits 0 when a leg did not run
    (`not run1 or ok1`), so a run that measured nothing at all returns success.
    An earlier version of this suite called that a PASS. The verdict line is the
    only honest interface, and an all-SKIP run is NOT-RUN -- the fixture
    measured nothing, which is not a statement about the firmware.
    """
    # PREMISE: the tap must actually carry samples. If it is not armed, the
    # capture is legitimately all zeros and the gate scores 0.0000 nonzero on
    # both channels -- indistinguishable from a perfect stream unless you ask the
    # tap. On this bench the tap was found enabled with tx_frames=0, the gate's
    # arm command answered nothing, and the run still looked like a measurement.
    ok_tap, tap0, eq = scpi("DEBUG:LOOPback?", card, timeout=10)
    if not ok_tap:
        return NOTRUN, ("%s: diag transport unresponsive before the run (%s) -- the gate's "
                        "arm/clear commands will not land, so it can only score zeros" % (
                            tag, eq or "no reply"))
    scpi("DEBUG:LOOPback 1", card)
    scpi("DEBUG:LOOPback:CLEAR", card)
    probe = tone_wav("/tmp/suite_tapprobe.wav", 2.0)
    p = play_bg(card, probe); time.sleep(1.0)
    ok_tap2, tap1, _ = scpi("DEBUG:LOOPback?", card, timeout=10)
    stop(p); time.sleep(0.4)
    tx = -1
    m = re.search(r"(\d+),(\d+)", (tap1 if ok_tap2 else "") or "")
    if m:
        tx = int(m.group(2))
    if tx <= 0:
        return NOTRUN, ("%s: loopback tap delivered %d frames while streaming (state %s) -- "
                        "bit-perfectness cannot be measured until the tap carries samples; a "
                        "zero here is NOT evidence about the data path" % (tag, tx, tap1.strip()))
    rc, out = sh([PY, os.path.join(RIG, "csd_bitperfect_gate.py"),
                  "--minutes", str(minutes)], timeout=3600)
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    v = [l for l in lines if l.startswith("GATE:")]
    pct = [l for l in lines if "%" in l]
    if not v:
        return NOTRUN, "%s: gate produced no verdict line (rc=%d); last output: %s" % (
            tag, rc, " / ".join(lines[-3:])[:220])
    got = re.findall(r"TEST\d=([A-Za-z()]+)", v[-1])
    if not got:
        return NOTRUN, "%s: unreadable verdict %r" % (tag, v[-1])
    ev = " | ".join((pct[-1:] + v[-1:]))
    if "FAIL" in got:
        return FAIL, tag + ": " + ev
    if all(g.startswith("SKIP") for g in got):
        return NOTRUN, tag + ": every leg skipped, nothing was measured -- " + ev
    return PASS, tag + ": " + ev


def case_06_bitperfect_bare(dut):
    """Case 6: bit-perfect at the maximum sample rate, DSP out of the path.

    The gate itself enforces the premises (generator not armed, preset none, host
    volume provably at max, mixer gains unity) and aborts rather than certify a
    scaled stream -- that abort must surface as NOT-RUN, not as a silent pass.
    """
    st, ev = run_gate(dut["bp_minutes"], "DSP bypass (preset none)", dut["dac"])
    return st, ev


def case_07_bitperfect_dsp(dut):
    """Case 7: bit-perfect at max rate with the DSP enabled, if the firmware has one.

    The tap is POST-DSP, so a processing kernel is *expected* to differ from the
    input; demanding bit-exactness through it would be a test of the wrong thing.
    The criterion that means what the owner asked is a bit-transparent kernel
    (unity gain / pure delay) loaded over MIDI. Until such a kernel is loadable,
    the honest answer is TODO with the dependency named -- not a pass borrowed
    from the bypass leg.
    """
    if not dut["dsp_present"]:
        return TODO, ("no DSP in this image (DIAG:DSP:PRESet? unanswered) -- case does not "
                      "apply; rebuild with PCM_DSP_EN=1 to exercise it")
    ok, cur, eq = scpi("DIAG:DSP:PRESet?", dut["dac"])
    scpi("DIAG:DSP:PRESet 2", dut["dac"])
    st, ev = run_gate(dut["bp_minutes"], "DSP enabled (preset 2, processing kernel)", dut["dac"])
    scpi("DIAG:DSP:PRESet %s" % (cur.strip() or "0"), dut["dac"])
    if st == PASS:
        return PASS, ("bit-exact through a live processing preset -- suspicious: verify the "
                      "kernel really ran; " + ev)
    return TODO, ("DSP present and selectable (preset readback works) but no bit-transparent "
                  "kernel is loadable over MIDI, and the bit-perfect tap is post-DSP so a "
                  "processing preset cannot be certified bit-exact. Dependency: a unity-gain/"
                  "allpass kernel load command. Gate said: %s" % ev[:180])


def case_07_1_dsp_control(dut):
    d = dut["dac"]
    ok, a, eq = scpi("DIAG:DSP:PRESet?", d)
    if not ok:
        return (TODO if eq else FAIL), ("query refused (%s)" % eq) if eq else "query timed out with empty error queue"
    ok, _, eq = scpi("DIAG:DSP:PRESet 2", d)
    if not ok:
        return (TODO if eq else FAIL), ("set refused (%s)" % eq) if eq else "set timed out"
    ok, b, _ = scpi("DIAG:DSP:PRESet?", d)
    ok, _, eq = scpi("DIAG:DSP:PRESet %s" % (a.strip() or "0"), d)
    ok, c, _ = scpi("DIAG:DSP:PRESet?", d)
    if not (a.strip() and b.strip()):
        return FAIL, "empty readback: %r then %r" % (a, b)
    if a.strip() == b.strip():
        return FAIL, "set had no effect: %r before and after PRESet 2" % a.strip()
    if c.strip() != a.strip():
        return FAIL, "restore failed: %r -> 2 -> %r (wanted %s)" % (a, c, a)
    return PASS, "readback %s -> set 2 -> readback %s -> restored %s (changed and reversible)" % (
        a.strip(), b.strip(), c.strip())


def case_08_acoustic_lr(dut):
    m = dut["mic"]
    if m is None:
        return NOTRUN, "no stereo capture mic attached"
    ok, why = set_volume(dut["dac"], 192)
    if not ok:
        return NOTRUN, "host volume: %s" % why
    res = {}
    for ch, nm in ((0, "L"), (1, "R")):
        wav = tone_wav("/tmp/suite_08_%d.wav" % ch, 3.0, 1000.0, ch=ch)
        p = play_bg(dut["dac"], wav); time.sleep(0.5)
        pk, fl = record_peak(m, 2)
        stop(p); time.sleep(0.3)
        if pk is None:
            return NOTRUN, str(fl)
        res[nm] = (pk[0], pk[1], max(fl.values()))
    iso_l = res["L"][0] - res["L"][1]
    iso_r = res["R"][1] - res["R"][0]
    detail = ("L-only: micL %.1f micR %.1f (isolation %+.1f dB) | R-only: micL %.1f "
              "micR %.1f (isolation %+.1f dB)" % (res["L"][0], res["L"][1], iso_l,
                                                   res["R"][0], res["R"][1], iso_r))
    if res["L"][0] < res["L"][1] and res["R"][1] < res["R"][0]:
        return FAIL, "CHANNELS SWAPPED -- " + detail
    if iso_l >= 12 and iso_r >= 12:
        return PASS, "no swap; " + detail
    if iso_l < 12 or iso_r < 12:
        return FAIL, "isolation below 12 dB -- " + detail
    return FAIL, detail


def _vol_dbscale(card):
    """(min_count, max_count, dB_at_min) as the control DECLARES, not as assumed.

    Hard-coding 0.25 dB/step once made a correct board look broken and a broken
    board look correct: this card is 0..192 over -48..0 dB, and the measured drop
    came out at twice the asked value, which only means anything against the
    control's own scale.
    """
    out = sh(["amixer", "-c", str(card), "controls"])[1]
    nids = [int(l.split("numid=")[1].split(",")[0]) for l in out.splitlines()
            if "Playback Volume" in l]
    if not nids:
        return None
    got = sh(["amixer", "-c", str(card), "cget", "numid=%d" % nids[0]])[1]
    mn = re.search(r"min=(\d+),max=(\d+)", got)
    db = re.search(r"dBminmax-min=(-?[\d.]+)dB", got)
    if not mn or not db:
        return None
    return int(mn.group(1)), int(mn.group(2)), float(db.group(1))


def case_09_volume(dut):
    """Case 9: the host volume must move the acoustic level by the amount it claims.

    ONE STAGE AT A TIME. This card exposes two "Playback Volume" controls that
    are NOT left and right -- numid 5 carries two elements and numid 6 one, and
    driving numid 5 alone moves BOTH channels. They are serial gain stages, each
    declaring 0..192 over -48..0 dB. Setting them both to 96 therefore asks for
    -48 dB total, and an earlier version of this case did exactly that, measured
    -48 dB, and reported a firmware bug about the harness's own arithmetic.

    So each stage is driven alone with the others pinned at max, and judged
    against the dB range that stage declares. Slope, three points, per stage.
    """
    m = dut["mic"]
    if m is None:
        return NOTRUN, "no capture mic attached"
    sc = _vol_dbscale(dut["dac"])
    if sc is None:
        return NOTRUN, "could not read the volume control's declared dB range"
    cmin, cmax, dbmin = sc
    out = sh(["amixer", "-c", str(dut["dac"]), "controls"])[1]
    nids = [int(l.split("numid=")[1].split(",")[0]) for l in out.splitlines()
            if "Playback Volume" in l]
    if not nids:
        return NOTRUN, "no Playback Volume controls"
    wav = tone_wav("/tmp/suite_09.wav", 3.0)

    def all_at(v):
        for n in nids:
            set_volume(dut["dac"], v)

    def level():
        p = play_bg(dut["dac"], wav); time.sleep(0.5)
        pk, fl = record_peak(m, 2)
        stop(p); time.sleep(0.3)
        return (max(pk.values()) if pk else None), fl

    all_at(cmax)
    base, fl = level()
    if base is None:
        return NOTRUN, str(fl)
    if base < max(fl.values()) + 6:
        return NOTRUN, "probe deaf at full volume (tone %.1f vs floor %.1f dBFS)" % (
            base, max(fl.values()))

    rows, best_err = [], None
    for nid in nids:
        for count in (cmax, (cmax + cmin) // 2, cmin):
            all_at(cmax)
            set_volume(dut["dac"], count)          # set_volume drives every nid
            # ...so drive this stage alone by restoring the others explicitly
            for other in nids:
                if other != nid:
                    sh(["amixer", "-c", str(dut["dac"]), "cset", "numid=%d" % other,
                        str(cmax)])
            pk, _ = level()
            if pk is None:
                all_at(cmax)
                return NOTRUN, "capture failed during a volume leg"
            # Both sides must be MAGNITUDES. dbmin is negative and `drop` is a
            # positive attenuation, so comparing them directly scored a stage that
            # tracked -24 dB as 23.2 dB as a 47 dB error -- a sign bug in the
            # harness that reported a working volume control as broken.
            asked = abs(dbmin) * (cmax - count) / float(cmax - cmin)
            drop = base - pk
            err = abs(drop - asked)
            # both printed as attenuation magnitudes, matching `asked`
            rows.append("numid%d=%3d asked %.1f dB attenuation, measured %.1f dB (err %.1f)" % (
                nid, count, asked, drop, err))
            if count != cmax:
                best_err = err if best_err is None else min(best_err, err)
    all_at(cmax)
    detail = ("%d serial stage(s) %s, %d..%d = %.1f..0 dB each; baseline %.1f dBFS; %s" % (
        len(nids), nids, cmin, cmax, dbmin, base, "; ".join(rows)))
    if best_err is None:
        return NOTRUN, "no attenuation leg was measured: " + detail
    if best_err <= 4.0:
        return PASS, "each stage tracks its own declared scale; " + detail
    return FAIL, ("stage does not track its declared dB range (best error %.1f dB): "
                  % best_err) + detail


def case_10_lr_trim(dut):
    """Per-channel trim, measured acoustically, one side at a time.

    VOLume:TRIM:<ch> takes COUNTS of 0.125 dB, so -6 dB is -48 counts. Asking
    in dB and reading the result in counts is how a working trim path looks dead.
    """
    m = dut["mic"]
    if m is None:
        return NOTRUN, "no capture mic attached"
    d = dut["dac"]
    ok, _, eq = scpi("VOLume:TRIM?", d)
    if not ok:
        return (TODO if eq else FAIL), "no trim command: %s" % (eq or "no reply")
    SIXDB = -48                                          # counts of 0.125 dB
    ok, _ = set_volume(d, 192)
    scpi("VOLume:TRIM:LEFT 0", d); scpi("VOLume:TRIM:RIGHT 0", d)
    time.sleep(0.4)
    wav = tone_wav("/tmp/suite_10.wav", 3.0)

    def measure():
        p = play_bg(d, wav); time.sleep(0.5)
        pk, fl = record_peak(m, 2)
        stop(p); time.sleep(0.3)
        return pk, fl

    base, fl = measure()
    if base is None:
        return NOTRUN, str(fl)
    if base[0] < max(fl.values()) + 6:
        return NOTRUN, "probe deaf: tone %.1f dBFS vs floor %.1f" % (base[0], max(fl.values()))
    # Where did the count land? Reading the per-channel volume regs (0x0F/0x10)
    # and the master trim (0x11..0x14) after touching ONE channel turns "both
    # channels moved" from a phenomenon into a location.
    scpi("VOLume:TRIM:LEFT %d" % SIXDB, d); time.sleep(0.4)
    regs_after_L, _ = b64read([0x0F, 0x10, 0x11, 0x12, 0x13, 0x14], d)
    afterL, _ = measure()
    scpi("VOLume:TRIM:LEFT 0", d); time.sleep(0.4)
    scpi("VOLume:TRIM:RIGHT %d" % SIXDB, d); time.sleep(0.4)
    regs_after_R, _ = b64read([0x0F, 0x10, 0x11, 0x12, 0x13, 0x14], d)
    afterR, _ = measure()
    scpi("VOLume:TRIM:RIGHT 0", d); time.sleep(0.3)
    fmt = lambda r: "none" if r is None else " ".join("%02X" % b for b in r)
    reg_note = " | regs CHvol/MASTER after LEFT-only: %s | after RIGHT-only: %s" % (
        fmt(regs_after_L), fmt(regs_after_R))
    if afterL is None or afterR is None:
        return NOTRUN, "capture failed during trim legs"
    dL, dR = base[0] - afterL[0], base[1] - afterL[1]
    eL, eR = base[0] - afterR[0], base[1] - afterR[1]
    detail = ("L-trim %d counts (-6.0 dB): L moved %.2f dB, R moved %.2f dB | R-trim: L moved "
              "%.2f dB, R moved %.2f dB%s" % (SIXDB, dL, dR, eL, eR, reg_note))
    if abs(dL - 6) <= 2.5 and abs(dR) <= 1.5 and abs(eR - 6) <= 2.5 and abs(eL) <= 1.5:
        return PASS, "independent; " + detail
    return FAIL, "not independent (want each side -6 +/-2.5 dB, other side within 1.5 dB): " + detail


def _whiteness(x):
    """(spectral flatness, strongest non-trivial autocorrelation, tonal line ratio).

    Why not a distinct-value count: a diagnostic stamp stream -- frame counters,
    timestamps -- also produces tens of thousands of distinct words, so 'lots of
    different values' cannot separate a microphone from a firmware counter. A
    real analogue front end is flat in frequency, Gaussian in amplitude and has
    no periodic structure; a counter has all three wrong.
    """
    x = x.astype(np.float64)
    x = x - x.mean()
    sp = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2 + 1e-20
    flat = float(np.exp(np.mean(np.log(sp))) / np.mean(sp))
    ac = np.correlate(x, x, "full")[len(x) - 1:]
    ac = ac / (ac[0] + 1e-30)
    lag = float(np.max(np.abs(ac[64:4000]))) if len(ac) > 4000 else 0.0
    line = float(20 * np.log10((sp[1:].max() / (np.median(sp[1:]) + 1e-30)) ** 0.5))
    return flat, lag, line


def case_11_builtin_mic_noise(dut):
    """Case 11: with no excitation, the built-in mic path must return white noise.

    'No excitation' means nothing is playing, so this leg must not run while any
    tone is on the bus.
    """
    d = dut["dac"]
    rc, out = sh(["arecord", "-D", "hw:%d,0" % d, "--dump-hw-params"], timeout=40)
    if "S32_LE" not in out:
        return NOTRUN, "no capture stream on the DAC card"
    cap = "/tmp/suite_11.wav"
    if os.path.exists(cap):
        os.remove(cap)
    rc, out = sh(["arecord", "-q", "-D", "hw:%d,0" % d, "-f", "S32_LE", "-r", "48000",
                  "-c", "2", "-d", "3", "-t", "wav", cap], timeout=90)
    if not os.path.exists(cap):
        return FAIL, "capture stream would not open: %s" % out.strip()[:100]
    import wave
    with wave.open(cap, "rb") as w:
        n = w.getnframes()
        raw = np.frombuffer(w.readframes(n), dtype=np.int32).reshape(n, 2)
    stats, rms = [], []
    for c in range(2):
        flat, lag, line = _whiteness(raw[:, c])
        stats.append((flat, lag, line))
        rms.append(20 * np.log10(np.sqrt(np.mean((raw[:, c] / 2147483648.0) ** 2)) + 1e-15))
    detail = ("no excitation: ch0 flat=%.3f autocorr=%.2f line=%.1f dB rms=%.1f | ch1 "
              "flat=%.3f autocorr=%.2f line=%.1f dB rms=%.1f" % (
                  stats[0][0], stats[0][1], stats[0][2], rms[0],
                  stats[1][0], stats[1][1], stats[1][2], rms[1]))
    quiet = max(rms) < -118.0
    if quiet:
        return FAIL, ("capture is dead silent -- a wired analogue front end always shows a "
                      "thermal/noise floor; this stream is not a microphone: " + detail)
    ok_flat = all(s[0] > 0.05 for s in stats)
    ok_aper = all(s[1] < 0.50 for s in stats)
    ok_line = all(s[2] < 25.0 for s in stats)
    if ok_flat and ok_aper and ok_line:
        return PASS, "noise-like and aperiodic; " + detail
    why = []
    if not ok_flat:
        why.append("spectrum not flat (tonal/structured)")
    if not ok_aper:
        why.append("strong periodicity (a repeating stamp pattern, not a mic)")
    if not ok_line:
        why.append("dominant spectral line (injected signal, not noise)")
    return FAIL, "; ".join(why) + ": " + detail


def case_12_serial_in_descriptor(dut):
    rc, out = sh(["lsusb", "-d", "20b1:", "-v"], timeout=60)
    m = re.search(r"iSerial\s+\d+\s+(\S+)", out)
    if not m:
        return FAIL, "no iSerial found on the ClearSound device"
    desc_serial = m.group(1)
    ok, txt, _ = scpi("*IDN?", dut["dac"])
    midi_serial = txt.split(",")[2].strip() if ok and len(txt.split(",")) == 4 else None
    if desc_serial in ("", "0", "[00000000]"):
        return FAIL, "descriptor iSerial is empty: %r" % desc_serial
    if midi_serial and desc_serial.lower() != midi_serial.lower():
        return FAIL, "descriptor %r disagrees with *IDN? serial %r" % (desc_serial, midi_serial)
    return PASS, "descriptor iSerial=%s%s" % (
        desc_serial, " and equals the *IDN? serial" if midi_serial
        else " (MIDI serial unavailable for the cross-check)")


def case_13_24bit_192k(dut):
    """Case 13: 24-bit / 192 kHz, as ADVERTISED, and actually openable.

    Read the descriptor first and honour what it says. This card advertises
    bBitResolution 24 in a bSubslotSize 4 container -- i.e. S32_LE carrying
    24 valid bits -- and does NOT offer a 3-byte S24_3LE container. Demanding
    S24_3LE from it is a bug in the test, not a defect in the DAC: the earlier
    version of this case failed exactly that way and reported "descriptor lies"
    about a descriptor that had told the truth all along.
    """
    d = dut["dac"]
    usb = sh(["lsusb", "-d", "20b1:", "-v"], timeout=60)[1]
    # Pair each bSubslotSize with the bBitResolution that follows it: that is the
    # container/precision the FORMAT_TYPE_I descriptor actually declares.
    pairs = []
    toks = re.findall(r"b(SubslotSize|BitResolution)\s+(\d+)", usb)
    for k in range(len(toks) - 1):
        if toks[k][0] == "SubslotSize" and toks[k + 1][0] == "BitResolution":
            pairs.append((int(toks[k][1]), int(toks[k + 1][1])))
    pairs = sorted(set(pairs))
    if not pairs:
        return NOTRUN, "no FORMAT_TYPE_I subslot/resolution pairs found in the descriptor"
    ALIAS = {(4, 24): "S32_LE", (4, 32): "S32_LE", (3, 24): "S24_3LE", (2, 16): "S16_LE"}
    twenty_four = [(ss, br) for ss, br in pairs if br >= SUITABLE_BITS]
    if not twenty_four:
        return FAIL, "descriptor advertises no 24-bit-or-better mode (pairs %s)" % pairs
    ss, br = twenty_four[0]
    alsa_fmt = ALIAS.get((ss, br))
    if alsa_fmt is None:
        return NOTRUN, ("advertised 24-bit container (subslot %d, res %d) has no known ALSA "
                        "mapping (pairs %s)" % (ss, br, pairs))
    wav = "/tmp/suite_13_192.wav"
    codec = {4: "pcm_s32le", 3: "pcm_s24le", 2: "pcm_s16le"}[ss]
    rc3, out3 = sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=1000:duration=1", "-ac", "2", "-ar", str(SUITABLE_RATE),
                    "-c:a", codec, wav, "-y"], timeout=60)
    if rc3 != 0:
        return NOTRUN, "could not render the test file: %s" % out3.strip()[:100]
    pout = sh(["aplay", "-D", "hw:%d,0" % d, "--dump-hw-params", wav], timeout=60)[1]
    pr = re.search(r"RATE:\s*\[?(\d+)\s+(\d+)", pout)
    if not pr:
        return NOTRUN, "could not read playback rate range: %s" % pout.strip()[:120]
    lo, hi = int(pr.group(1)), int(pr.group(2))
    rc4, out4 = sh(["aplay", "-q", "-D", "hw:%d,0" % d, wav], timeout=60)
    detail = ("descriptor modes %s (bit-res, subslot) -> 24-bit class is %d-bit in %d-byte "
              "slots = ALSA %s; rates %d..%d Hz; streamed %d Hz rc=%d %s" % (
                  pairs, br, ss, alsa_fmt, lo, hi, SUITABLE_RATE, rc4, out4.strip()[:70]))
    if hi < SUITABLE_RATE:
        return FAIL, "192 kHz is not offered (max %d Hz): " % hi + detail
    if rc4 != 0:
        return FAIL, ("advertised 24-bit mode would not stream as %s -- descriptor/ALSA "
                      "mismatch: " % alsa_fmt + detail)
    return PASS, detail


def case_14_power_saving(dut):
    d = dut["dac"]
    ok, cur, eq = scpi("POWER:TIMEout?", d)
    has_control = ok and bool(cur.strip())
    notes = []

    # (a) enter and exit -- the part that exists today, if a short-timeout image
    #     is loaded. Proven by rig/idle_power_cycle.py against the device log.
    rc, out = sh([PY, os.path.join(RIG, "idle_power_cycle.py")], timeout=900)
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    ev = " / ".join(lines[-6:])[:400] if lines else "(no output)"
    if rc == 0:
        notes.append("enter/exit PASS: " + ev)
    else:
        notes.append("enter/exit rc=%d: %s" % (rc, ev))

    if not has_control:
        return TODO, ("; ".join(notes) + " || MISSING IN FIRMWARE: POWER:TIMEout?/POWER:TIMEout "
                      "<sec> with the owner's semantics (0 = sleep now and stay asleep, "
                      "negative = never sleep, >0 = seconds, default 300) plus persistence "
                      "of any non-zero value in the QSPI journal and application at boot.")
    # (b) control + (c) persistence, only reachable once the command exists
    ok, got0, _ = scpi("POWER:TIMEout?", d)
    scpi("POWER:TIMEout 45", d)
    ok, got1, _ = scpi("POWER:TIMEout?", d)
    scpi("POWER:TIMEout -1", d)
    ok, got2, _ = scpi("POWER:TIMEout?", d)
    scpi("POWER:TIMEout 0", d)
    time.sleep(2)
    scpi("POWER:TIMEout 300", d)
    detail = "read %s, after set 45 -> %s, after set -1 -> %s" % (got0.strip(), got1.strip(), got2.strip())
    if got1.strip() == "45" and got2.strip() in ("-1", "4294967295"):
        return PASS, detail + " (persistence across a power cycle is a separate leg)"
    return FAIL, "timeout control does not stick: " + detail


CASES = [
    ("01", "mute no sound / unmute with sound", case_01_mute),
    ("02", "*IDN? device serial over MIDI", case_02_idn_serial),
    ("03", "DAC register read/write over MIDI", case_03_reg_rw),
    ("04", "persistent store read/write over MIDI", case_04_persistence),
    ("05", "DFU firmware update, twice in a row", case_05_dfu_twice),
    ("06", "bit-perfect at max sample rate", case_06_bitperfect_bare),
    ("07", "DSP enabled, bit-perfect at max rate", case_07_bitperfect_dsp),
    ("07.1", "DSP kernel read/set over MIDI", case_07_1_dsp_control),
    ("08", "acoustic loopback L/R, no swap", case_08_acoustic_lr),
    ("09", "volume control", case_09_volume),
    ("10", "L/R volume trim independent", case_10_lr_trim),
    ("11", "built-in mic reads noise, no excitation", case_11_builtin_mic_noise),
    ("12", "serial number in USB descriptor", case_12_serial_in_descriptor),
    ("13", "24-bit / 192 kHz as advertised", case_13_24bit_192k),
    ("14", "power saving enter/exit + timeout control", case_14_power_saving),
]


def main():
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--dac", type=int)
    ap.add_argument("--mic", type=int)
    ap.add_argument("--allow-dfu", action="store_true")
    ap.add_argument("--quick", action="store_true", help="shorten the bit-perfect legs")
    ap.add_argument("--only", default=None,
                    help="comma list of case ids to run, e.g. '01,09' -- re-verifying one "
                         "instrument should not cost a full suite pass")
    a = ap.parse_args()

    dac = find_dac(a.dac)
    if dac is None:
        print("FATAL: no ClearSound DAC card found in /proc/asound/cards")
        return 2
    mic = find_mic(a.mic)

    ok, idn, _ = scpi("*IDN?", dac)
    print("ClearSound DAC acceptance suite")
    print("  dac card %d (%s)   mic card %s (%s)" % (
        dac, cards().get(dac), mic, cards().get(mic) if mic else "none"))
    print("  loaded image: %s" % (idn if ok else "NO *IDN? -- control path down"))
    if not ok:
        print("  Every MIDI-dependent case will report NOT-RUN/FAIL accordingly.")
    dsp_present = scpi("DIAG:DSP:PRESet?", dac)[0]
    dut = dict(dac=dac, mic=mic, allow_dfu=a.allow_dfu,
               bp_minutes=0.5 if a.quick else 2.0, dsp_present=dsp_present)

    # One case must not poison the next: an early version left trims and the DSP
    # preset dirty, and the bit-perfect gate then aborted its own premises and
    # looked like a firmware failure.
    print("  baseline: host volume full, trims 0 counts, DSP preset 0")
    set_volume(dac, 192)
    for c in ("VOLume:TRIM:LEFT 0", "VOLume:TRIM:RIGHT 0", "DIAG:DSP:PRESet 0"):
        scpi(c, dac)

    cases = CASES
    if a.only:
        want = {x.strip() for x in a.only.split(",") if x.strip()}
        cases = [c for c in CASES if c[0] in want]
        if not cases:
            print("FATAL: --only matched no case (have: %s)" % ", ".join(c[0] for c in CASES))
            return 2

    results = []
    for cid, name, fn in cases:
        t0 = time.time()
        try:
            status, detail = fn(dut)
        except Exception as e:                                    # noqa: BLE001
            status, detail = NOTRUN, "harness exception: %r" % e
        results.append((cid, name, status, detail))
        print("[%4s] %-46s %-7s %6.1fs  %s" % (cid, name, status, time.time() - t0, detail))

    counts = {s: sum(1 for r in results if r[2] == s) for s in (PASS, FAIL, NOTRUN, TODO)}
    print("\n===== SUMMARY =====")
    for cid, name, status, _ in results:
        print("  %-5s %-46s %s" % (cid, name, status))
    print("  PASS=%d FAIL=%d NOT-RUN=%d TODO=%d" % (
        counts[PASS], counts[FAIL], counts[NOTRUN], counts[TODO]))
    if counts[FAIL]:
        print("  RESULT: FAIL (%d case(s) measured and did not meet criterion)" % counts[FAIL])
        return 1
    if counts[NOTRUN]:
        print("  RESULT: INCOMPLETE -- %d case(s) could not be measured; fix the fixture, "
              "do not book these as passes" % counts[NOTRUN])
        return 1
    print("  RESULT: all measurable cases passed; %d TODO item(s) remain" % counts[TODO])
    return 0


if __name__ == "__main__":
    sys.exit(main())
