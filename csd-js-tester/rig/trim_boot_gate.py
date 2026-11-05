#!/usr/bin/env python3
"""Boot-time MASTER TRIM acceptance gate (ES9219Q regs 0x11..0x14).

Why this exists: commit 2ed5dce added a boot writer for MASTER TRIM that posted
08 00 00 00, and the board was inaudible for four days while every other codec
register read healthy -- mute clear, DPLL locked, CHIP_STATUS fine, analog 0 dB.
Two independent defects hid behind those readings:

  1. byte order. The wire is LITTLE-ENDIAN: reg 0x11 is the LEAST significant
     byte. Measured 2026-11-05 by writing the same unity word under both orders:
     MSB-first changed nothing (-42 dBFS), LSB-first produced sound (+23 dB).
  2. unity scale. csd_trim_master_trim_q31() derived the codec word from the
     mixer's Q2.29 scale and shifted RIGHT by 2, so 0 dB became 0x08000000
     (= -30 dB as a Q31 fraction) instead of 0x7FFFFFFF.

A register-only check would have passed all four days, so this gate asserts BOTH
the bytes and that a tone is actually audible. Register bytes are what the codec
was TOLD; the microphone is what came out.

Exit: 0 pass, 1 fail, 2 fixture/transport fault (never a silent pass).
"""
import argparse, base64, os, subprocess, sys, time
import numpy as np
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
RIG = os.path.dirname(HERE)
CLIENT = os.path.join(RIG, "csd_diag.py")
TRIM = [0x11, 0x12, 0x13, 0x14]
UNITY_BYTES = [0xFF, 0xFF, 0xFF, 0x7F]      # 0x7FFFFFFF, reg17 = LSB
AUDIBLE_DBFS = -45.0                          # mic floor ~-72, deaf rig reads ~-56


def scan_dac_cards():
    """Cards whose name says ClearSound/XMOS UAC2.0 -- both build shapes."""
    out = subprocess.run(["cat", "/proc/asound/cards"], capture_output=True, text=True).stdout
    hits, num = [], None
    for line in out.splitlines():
        if line[:1].isdigit():
            num = int(line.split("[")[0].strip())
        elif num is not None:
            up = line.upper()
            if "UAC2" in up and any(k in up for k in ("XMOS", "CLEARSOUND", "SPECIALTYCIRCUITS")):
                hits.append(num)
    return hits


def scpi(cmd, card, timeout=20):
    # No --card here on purpose: csd_diag.py has no such option (it resolves the
    # raw MIDI node itself, /dev/snd/midiC<D>D0). Passing one makes argparse
    # exit 2 with usage on stderr, stdout comes back EMPTY, and a healthy board
    # is then reported as "not reachable" -- a fixture bug wearing a firmware
    # costume. Card selection stays available to the caller for the ALSA side.
    p = subprocess.run([sys.executable, CLIENT, "--backend", "rawmidi",
                        "--timeout", str(timeout), cmd],
                       capture_output=True, text=True, cwd=RIG, timeout=timeout + 30)
    return (p.stdout or "").strip().strip('"')


def read(addrs, card):
    out = scpi('REGister:READ? "%s"' % base64.b64encode(bytes(addrs)).decode(), card)
    tok = out.split()[-1] if out else ""
    try:
        return list(base64.b64decode(tok + "=" * ((4 - len(tok) % 4) % 4)))
    except Exception:
        print(f"  FAULT: unreadable reply {out!r}")
        sys.exit(2)


def force_full_volume(dac):
    """Playback Volume must be full or the level figure means nothing: 192 = 0 dB
    on a 0..192 range, and a stale partial value crushes both channels equally."""
    out = subprocess.run(["amixer", "-c", str(dac), "controls"],
                         capture_output=True, text=True).stdout
    nids = [int(l.split("numid=")[1].split(",")[0]) for l in out.splitlines()
            if "Playback Volume" in l]
    if not nids:
        print("  FAULT: no Playback Volume control found")
        sys.exit(2)
    for n in nids:
        subprocess.run(["amixer", "-c", str(dac), "cset", f"numid={n}", "192,192"],
                       capture_output=True)
        got = subprocess.run(["amixer", "-c", str(dac), "cget", f"numid={n}"],
                             capture_output=True, text=True).stdout
        # Take ONLY the values= field: scraping digits from the whole cget tail
        # also picks up range/interval numbers, so a control sitting at full
        # scale can read back as [2, 192, 192] and false-fail a good rig.
        import re as _re
        # Anchor on the ": values=" line. cget prints TWO values= fields: the
        # "; type=..." capability line carries values=N as the ELEMENT COUNT of
        # the control (a stereo control reports values=2), so a regex that
        # matches the first one reads a control at full scale as [2] and
        # false-fails the rig. Documented in the firmware skill; tripped on it
        # anyway, which is why the assertion is on the ": " line, not "values=".
        m = _re.search(r":\s*values=([0-9][0-9,\s]*)", got)
        vals = [int(x) for x in (m.group(1).replace(",", " ").split() if m else [])]
        if not vals or min(vals) < 192:
            print(f"  FAULT: numid {n} did not take full scale: {vals}")
            sys.exit(2)
    return nids


def tone_peak_dbfs(dac, mic, secs=3):
    wav = "/tmp/trim_gate_tone.wav"
    with wave.open(wav, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
        n = 48000 * (secs + 1)
        t = np.arange(n) / 48000.0
        x = (0.5 * 32767 * np.sin(2 * np.pi * 1000 * t)).astype(np.int16)
        st = np.empty(n * 2, dtype=np.int16); st[0::2] = x; st[1::2] = x
        w.writeframes(st.tobytes())
    cap = "/tmp/trim_cap.wav"
    if os.path.exists(cap):
        os.remove(cap)
    play = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
                             "-i", wav, "-f", "alsa", "-ac", "2", f"plughw:{dac},0"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.4)
    subprocess.run(["arecord", "-q", "-D", f"hw:{mic},0", "-f", "S24_3LE", "-r", "48000",
                    "-c", "2", "-d", str(secs), "-t", "wav", cap], capture_output=True, timeout=60)
    play.kill()
    with wave.open(cap, "rb") as w:
        n, ch = w.getnframes(), w.getnchannels()
        d = np.frombuffer(w.readframes(n), dtype=np.uint8)
    if ch != 2 or d.size != n * 6:
        print(f"  FAULT: capture {d.size}B/{n}f/{ch}ch")
        sys.exit(2)
    raw = d.reshape(n, 2, 3).astype(np.uint32)
    v = (raw[:, :, 0] | (raw[:, :, 1] << 8) | (raw[:, :, 2] << 16)).astype(np.int32)
    v = np.where(v & 0x800000, v - (1 << 24), v).astype(np.float64) / (1 << 23)
    return max(20 * np.log10(np.max(np.abs(v[:, c])) + 1e-12) for c in (0, 1))


def main():
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--dac", type=int, required=True)
    ap.add_argument("--mic", type=int, required=True)
    ap.add_argument("--secs", type=int, default=3)
    a = ap.parse_args()

    # A load via xrun re-enumerates USB, and the raw MIDI node can lag the ALSA
    # card by several seconds; one immediate *IDN? then reads as "board not
    # reachable" and aborts a healthy board. Retry with backoff, and fall back
    # to resolving the card by model string -- card numbers are assignment
    # order, not identity, and they move on every re-enumeration.
    idn, card = "", a.dac
    for attempt in range(1, 9):
        for cand in [a.dac] + scan_dac_cards():
            idn = scpi("*IDN?", cand, 10)
            if idn:
                card = cand
                break
        if idn:
            break
        time.sleep(4)
    if not idn:
        print("  FAULT: no *IDN? after 8 attempts -- board not reachable, no verdict")
        sys.exit(2)
    if card != a.dac:
        print(f"  note: DAC resolved to card {card}, not the requested {a.dac}")
    a.dac = card
    print(f"  image: {idn}")

    # Read the trim ALONGSIDE two registers whose values boot-time init fixes
    # (0x03=0x40, 0x07=0x00). A codec held in reset answers I2C with zeros, so an
    # all-zero trim read means one of two very different things: the analog gain
    # really is zero, or nothing is answering on the bus. The neighbours settle
    # it without writing anything. This is not hypothetical -- a post-wake run
    # convicted the board of 00 00 00 00 while the microphone reported full
    # level, because the read landed before the wake re-init had finished.
    WITNESS, EXPECT_WIT = [0x03, 0x07], [0x40, 0x00]
    got = None
    for _ in range(6):
        blob = read(WITNESS + TRIM, a.dac)
        if (len(blob) == len(WITNESS) + 4 and blob[:2] == EXPECT_WIT
                and blob[2:] != [0, 0, 0, 0]):
            got = blob[2:]
            break
        time.sleep(1)
    if got is None:
        # Still zero after retrying. Trust it only if the witnesses answered
        # correctly in the same transaction; otherwise the bus is the liar and
        # the board gets no verdict.
        blob = read(WITNESS + TRIM, a.dac)
        if len(blob) != len(WITNESS) + 4 or blob[:2] != EXPECT_WIT:
            print("  FAULT: witness regs %s did not read %s -- the I2C read is not "
                  "authoritative, no verdict"
                  % (["%02X" % x for x in WITNESS], ["%02X" % x for x in EXPECT_WIT]))
            sys.exit(2)
        got = blob[2:]
    ok_bytes = got == UNITY_BYTES
    print(f"  MASTER TRIM reg17..20 = {' '.join('%02X' % b for b in got)}"
          f"   expect {' '.join('%02X' % b for b in UNITY_BYTES)} (0x7FFFFFFF, reg17=LSB)")

    force_full_volume(a.dac)
    peak = tone_peak_dbfs(a.dac, a.mic, a.secs)
    ok_aud = peak > AUDIBLE_DBFS
    print(f"  1 kHz acoustic peak = {peak:+.1f} dBFS   need > {AUDIBLE_DBFS:+.1f}")

    ok = ok_bytes and ok_aud
    print(f"TRIM BOOT GATE: {'PASS' if ok else 'FAIL'}"
          + ("" if ok else "  (bytes and audibility are two separate defects -- fix both)"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
