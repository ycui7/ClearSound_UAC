#!/usr/bin/env python3
"""Does the case-A artifact follow AudioHwConfig(), or follow "first stream"?

lib_xua runs the user bring-up (AudioHwConfig_Mute -> AudioHwConfig ->
AudioHwConfig_UnMute) only when the sample rate changes or on the very first
stream (xua_audiohub.xc `firstRun` / the rate-change block). Every later
stream at the same rate skips it. If the artifact is produced by the chip's
reaction to that bring-up sequence, it must reappear on a FORCED re-config
(48k -> 44.1k -> 48k) without a cold load. If it does not, the trigger is
first-open-only (USB prefill / chip cold start) and the bring-up theory dies.

Three measurement defects this file used to have -- each produced a
confident, WRONG verdict, so all three are instrumented now:

  1. opened plughw:, which RESAMPLES silently: a 44.1k file can reach the
     endpoint as 48k, no SET_CUR, AudioHwConfig never runs -- the 're-configs
     are clean' conclusion was void. MIXer:HWCfg? now proves every leg.
  2. switching to hw: without checking the exit code: ffmpeg died with its
     stderr hidden and all five legs measured an idle bench (counter flat at
     10, identical values everywhere). Every leg now checks ffmpeg's rc AND
     /proc/asound hw_params and reports INVALID rather than clean.
  3. judged the 1 kHz bin against the 2-16 kHz FLOOR: the bench carries an
     ambient ~1 kHz bump ~7 dB above that floor in EVERY state including
     no-stream, so 'delta >= 6' was permanently true. The reference is now
     the A0 no-stream leg's OWN 1 kHz bin.

A leg CARRIES the artifact when its 1 kHz bin exceeds A0's by >= 6 dB.
A leg is INVALID (not clean) unless ffmpeg rc==0 and, for re-config legs,
the hwcfg counter advanced.

Usage: python3 rig/rate_change_replay.py
"""
from __future__ import annotations
import os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ears_matrix as EM

APP = os.path.dirname(HERE)
PY = f"{APP}/.venv/bin/python"
CAP_D = 4           # arecord seconds; artifact lives in the first ~250 ms
RATE = 48000


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def hwcfg():
    try:
        return tuple(int(x) for x in scpi("MIXer:HWCfg?").split(","))
    except ValueError:
        return None


def hw_params():
    """Playback substream state right now (rate/format, or 'closed')."""
    for c in sorted(os.listdir("/proc/asound")):
        # card<number>/card does NOT exist on this kernel; the name lives in
        # card<number>/id (e.g. 'XMOSUAC20'). Matching a nonexistent file made
        # this return '?' and every stream leg was then marked INVALID -- while
        # the verdict logic still drew a conclusion from them.
        if "XMOS" not in open(f"/proc/asound/{c}/id").read():
            continue
        p = f"/proc/asound/{c}/pcm0p/sub0/hw_params"
        try:
            return open(p).read().strip().replace("\n", " ")
        except OSError:
            pass
    return "?"


def tone_at_1k(wav):
    """dBFS of the rig tone, max over channels, via the SHARED bin math in
    ears_matrix (freq*nper/sr -- never len(db), see case_a_fingerprint)."""
    r = EM.analyse(wav, tones=(EM.TONE_HZ,))
    return max(r["L"].tone_db[EM.TONE_HZ], r["R"].tone_db[EM.TONE_HZ])


def zeros_wav(path, rate, secs):
    """All-zero stereo S24_3LE at EXACTLY `rate`, written here rather than
    synthesised by ffmpeg: the lavfi+pcm_s24le combination failed on hw:
    (rc=251, ALSA rejected the negotiated format), and a wav we generate
    cannot drift in format, rate or channel count. stdlib wave only."""
    import wave
    w = wave.open(path, "wb")
    w.setnchannels(2); w.setsampwidth(3); w.setframerate(rate)
    w.writeframes(b"\x00" * (2 * 3 * rate * int(secs)))
    w.close()
    return path


def stream(rate, secs):
    """Stream zeros through the pattern the ACOUSTIC GATE proves works on this
    box: pre-made S24_3LE wav -> plughw. Two failed experiments before this:
      * lavfi anullsrc input: ffmpeg produced no packets at all (rc 251
        'received no packets') -- a dead input side, NOT an ALSA fault;
      * hw: direct with -re/-stream_loop: 'Could not write header' likewise.
    plughw resamples silently in principle -- but the endpoint is now PROVEN
    per leg by /proc hw_params and the bring-up counter, so a silent
    44.1k->48k conversion shows up as INVALID (counter NO-RUN), and the
    'clean re-config' conclusion can no longer be vacuous."""
    wav = zeros_wav(os.path.join(TMP, f"z{rate}.wav"), rate, secs)
    # EXACT form from ears_matrix.record(), which streams tones through this
    # device all day: -re with a FINITE wav, so ffmpeg exits by itself. The
    # two failed variants (-stream_loop -1 with and without -re) never
    # returned, which turned every rc into a kill artifact and left the
    # stderr file unflushed when read.
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-i", wav,
         "-f", "alsa", "-ac", "2", f"plughw:{DAC},0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


_TAG = [""]


def tag_of(rate):
    return _TAG[0] or f"z{rate}"


def leg(tag, rate=None):
    """Mic capture FIRST (the artifact is the first ~250 ms of the open).
    rate=None -> A0: capture with NOTHING streamed, the ambient reference."""
    cap = os.path.join(TMP, f"{tag}.wav")
    EM.kill_orphan_arecords()
    n0 = hwcfg()
    p = subprocess.Popen(
        ["arecord", "-D", f"hw:{EARS},0", "-f", "S24_3LE", "-r", str(RATE),
         "-c", "2", "-d", str(CAP_D), "-t", "wav", cap],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    _TAG[0] = tag
    ff = None
    if rate is not None:
        ff = stream(rate, CAP_D + 0.5)
    p.wait()
    rc, hp, why = 0, "closed", ""
    if ff:
        hp = hw_params()               # sample rate the endpoint accepted
        try:
            rc = ff.wait(timeout=20)
        except subprocess.TimeoutExpired:
            ff.terminate(); rc = ff.wait(timeout=10)
        why = (ff.stderr.read().decode(errors="replace").strip()[-90:]
               if ff.stderr else "")
    tone = tone_at_1k(cap)
    n1 = hwcfg()
    ran = n1[0] > n0[0] if (n0 and n1) else None
    ok = True
    if rate is None:
        status = "A0-reference"
    elif rc != 0:
        status = f"INVALID ffmpeg-rc={rc} {why}"
        ok = False
    elif "closed" in hp:
        status = "INVALID stream-never-opened"
        ok = False
    elif str(rate) not in hp:
        # THE plughw trap: ALSA negotiated a DIFFERENT rate than asked
        # (resampled silently) -> no clock SET_CUR -> the leg tested nothing.
        status = f"INVALID endpoint-at-{hp.split()[0] if hp.split() else '?'} not {rate}"
        ok = False
    else:
        status = f"ok {hp}" + ("" if ran is None else
                               (" bring-up-RAN" if ran else " bring-up-skipped"))
        # A re-config leg only counts as valid if the bring-up actually ran;
        # '44.1k at the endpoint' with a frozen counter is impossible, but
        # belt and braces: without the SET_CUR proof the leg is void.
        if rate != RATE and ran is False:
            status = f"INVALID no-SET_CUR ({hp})"
            ok = False
    print(f"  {tag:18s} {('@'+str(rate)) if rate else '(no stream)':>10s}  "
          f"1kHz {tone:+6.1f} dBFS   {status}")
    return tone, ok


def main():
    global DAC, EARS, TMP
    idn = scpi("*IDN?")
    if "ClearSound" not in idn:
        print(f"ABORT: firmware is not ours ({idn!r})"); return 1
    print(f"LIVE BUILD: {idn}")
    DAC = EM.find_dac_card(None)
    EARS = EM.find_ears_card(None)
    TMP = EM.tempfile.mkdtemp(prefix="replay_")
    EM.ensure_capture_switch(EARS)
    EM.set_volume(DAC, EM.VOL_0DB, EM.VOL_0DB, TMP)
    print(f"hwcfg at start: {hwcfg()}")

    print("\n== legs (zeros, mic-first, one cold boot) ==")
    a0, _   = leg("a0-nostream")
    time.sleep(1.0)
    t1, ok1 = leg("1-first-48k", RATE)
    time.sleep(1.0)
    t2, ok2 = leg("2-second-48k", RATE)
    time.sleep(1.0)
    t3, ok3 = leg("3-reconfig-44.1k", 44100)
    time.sleep(1.0)
    t4, ok4 = leg("4-reconfig-48k", RATE)
    time.sleep(1.0)
    t5, ok5 = leg("5-again-48k", RATE)

    rows = [("first", t1, ok1), ("second", t2, ok2),
            ("reconfig-44.1k", t3, ok3), ("reconfig-48k", t4, ok4),
            ("again-48k", t5, ok5)]
    # An INVALID leg is VOID, not evidence: it must never be counted as clean
    # nor as a carrier. Last version concluded 'trigger is first-open only'
    # from five legs it had itself marked INVALID.
    void = [n for n, _, ok in rows if not ok]
    good = [(n, v) for n, v, ok in rows if ok]
    carriers = [(n, v) for n, v in good if v - a0 >= 6.0]
    print("\n== verdict (each VALID leg vs A0's own 1 kHz bin; carry = +6 dB) ==")
    print(f"  A0 {a0:+.1f}   " + "  ".join(f"{n} {v - a0:+.1f}" for n, v in good))
    if void:
        print(f"  -> ABORT: {len(void)} leg(s) INVALID ({', '.join(void)})."
              "\n     No conclusion -- a leg that never proved it streamed is"
              "\n     void, and 'no carrier among void legs' means nothing."
              "\n     Fix the stream path (see the three failure modes in the"
              "\n     stream() docstring) and re-run.")
        return 2
    if not carriers:
        print("  -> No leg exceeds A0: no artifact on this build (post-revert "
              "expected). Re-config legs additionally PROVED they re-configured.")
        return 0
    names = ", ".join(f"{n} (+{v - a0:.1f})" for n, v in carriers)
    if any("reconfig" in n for n, _ in carriers):
        print(f"  -> carriers: {names}. Re-configs carry it: the trigger IS the "
              "bring-up sequence -- bisect the writes inside csd_AudioHwConfig "
              "against the mic in ONE boot.")
    else:
        print(f"  -> carriers: {names}. Only the first-open leg carries it: "
              "trigger is the first USB open / chip cold start, NOT the "
              "bring-up path (re-config legs ran and stayed at A0).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
