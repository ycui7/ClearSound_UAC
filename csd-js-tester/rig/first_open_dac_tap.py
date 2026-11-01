#!/usr/bin/env python3
"""What are the DAC-BOUND words during the FIRST stream open?

Everything content-based is now falsified, on the failing artifact, per-load,
one variable at a time (rig/first_stream_arm.py):

    as shipped      L +11.7 R +12.9 dB over no-stream
    generator muted L +11.1 R +13.7   <- generator cannot be it
    host muted      L +24.9 R +26.9   <- host stream cannot be it
    later streams   L  -1.4 R  -2.1   <- same gains, clean

So the artifact exists only on the first stream after load. The remaining
question has exactly two answers, and they need different fixes:

  (a) the firmware puts 1 kHz words on the I2S bus for the first frames
      (a module with uninitialised state -> fix the module), or
  (b) the words are silence and the codec produces the tone itself when
      AudioHwConfig re-pokes it at stream open (-> fix the bring-up order).

This rig decides, and it uses an instrument already in the firmware: with
CSD_DSP_DIAG_LOOP the audio thread mirrors sampsFromUsbToAudio -- the words
that go out over I2S -- onto the USB capture path. Reading card 4's capture
in the first milliseconds of the FIRST open therefore reads the DAC-bound
bus, digitally, with no microphone in the loop.

Self-check: the same window must show a real tone when a real tone is
streamed, otherwise the mirror is not wired and a silent result means
nothing.
"""
from __future__ import annotations
import os, subprocess, sys, tempfile, time, wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ears_matrix as EM

def tap(dac, ears, secs, stim, path):
    """Capture the board's OWN capture stream (its mirror of the I2S-bound
    words) while streaming `stim` through playback.

    Deliberately NOT EM.record: that records the E.A.R.S. microphone. Here the
    sensor must be the firmware's own data -- no capsule, no acoustics, no
    cable -- so a tone in these samples is content the firmware made.
    """
    EM.kill_orphan_arecords()
    cap = subprocess.Popen(
        ["arecord", "-D", f"hw:{dac},0", "-f", "S32_LE", "-r", str(EM.RATE),
         "-c", "2", "-d", str(secs), "-t", "wav", path],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    launch_off = None
    if stim is not None:
        time.sleep(0.6)                    # let arecord be streaming first
        launch_off = time.monotonic()      # windows are measured from here
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-i", stim,
             "-f", "alsa", "-ac", "2", f"plughw:{dac},0"],
            capture_output=True, text=True)
    rc = cap.wait()
    err = (cap.stderr.read().decode(errors="replace") if cap.stderr else "").strip()
    if rc != 0 or not os.path.exists(path) or os.path.getsize(path) < 1000:
        raise SystemExit(f"mirror capture failed (rc={rc}): {err[:200]}")
    return path, launch_off

def maxabs_and_tone(path, off_frames=0):
    """off_frames: capture frames that elapsed BEFORE the stream launched, so
    the named windows mean 'since the stream started' -- the artifact lives in
    the first frames of the stream, not of the capture."""
    import numpy as np
    w = wave.open(path); n = w.getnframes(); sw = w.getsampwidth()
    raw = w.readframes(n); w.close()
    if sw == 4:
        x = np.frombuffer(raw, dtype="<i4").reshape(-1, 2)
        full = float(1 << 31)
    elif sw == 3:
        a = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.uint32)
        v = (a[:, 0] | (a[:, 1] << 8) | (a[:, 2] << 16)).astype(np.int32)
        v[v >= 1 << 23] -= 1 << 24
        x = v.reshape(-1, 2); full = float(1 << 23)
    else:
        raise SystemExit(f"unexpected capture width {sw}")
    out = {}
    wins = (("first100ms", 0, 4800), ("next400ms", 4800, 24000),
            ("tail", 24000, None))
    for nm, a0, a1 in wins:
        a0 += off_frames
        a1 = (a1 + off_frames) if a1 is not None else None
        if a0 < 0:
            continue
        sl = slice(a0, a1)
        seg = x[sl]
        if len(seg) == 0:
            continue
        out[nm] = {"maxabs": int(np.max(np.abs(seg))),
                   "rms_db": float(20 * np.log10(np.sqrt(np.mean(seg.astype(np.float64) ** 2)) / full + 1e-12))}
    # 1 kHz power in the first 100 ms
    seg = x[off_frames:off_frames + 4800, 0].astype(np.float64)
    if len(seg):
        win = np.hanning(2048)
        seg2 = seg[:2048] * win
        mag = np.abs(np.fft.rfft(seg2))
        f = np.fft.rfftfreq(2048, 1 / EM.RATE)
        k = int(round(EM.TONE_HZ / EM.RATE * 2048))
        band = slice(max(1, k - 3), k + 4)
        peak = float(mag[band].max()); ref = float(np.median(mag[1:]) + 1e-12)
        out["tone_1k_first100ms"] = {"peak_bin": peak, "vs_median_db": float(20 * np.log10(peak / ref))}
    return out

def main():
    out = subprocess.run(["./.venv/bin/python", "csd_diag.py", "--backend", "rawmidi",
                          "--timeout", "8", "*IDN?"], cwd=os.path.dirname(HERE),
                         capture_output=True, text=True, timeout=40).stdout.strip()
    if "ClearSound" not in out:
        print(f"ABORT: firmware is not ours ({out!r})"); return 1
    print(f"LIVE BUILD: {out}")
    print("NOTE: this artifact must be built with -DCSD_DSP_DIAG_LOOP "
          "(the gate build is); it mirrors I2S-bound words onto capture.")

    ears = EM.find_ears_card(None); dac = EM.find_dac_card(None)
    tmp = tempfile.mkdtemp(prefix="first_open_dac_")
    EM.ensure_capture_switch(ears)
    EM.set_volume(dac, EM.VOL_0DB, EM.VOL_0DB, tmp)

    zeros = os.path.join(tmp, "zeros.wav"); EM.write_tone24(zeros, 3.5, 0.0, 0.0)
    tone  = os.path.join(tmp, "tone.wav");  EM.write_tone24(tone, 3.5, 0.8, 0.8)

    print("\n== FIRST stream this board has ever opened: host zeros, mirror tap ==")
    r = tap(dac, ears, 3, zeros, os.path.join(tmp, "first.wav"))
    for k, vv in maxabs_and_tone(r).items():
        print(f"  {k:20s} {vv}")

    print("\n== SECOND zeros stream (control: the artifact only exists on the first) ==")
    r = tap(dac, ears, 3, zeros, os.path.join(tmp, "second.wav"))
    for k, vv in maxabs_and_tone(r).items():
        print(f"  {k:20s} {vv}")

    print("\n== SELF-CHECK: stream a real 1 kHz tone, the mirror must show it ==")
    r = tap(dac, ears, 3, tone, os.path.join(tmp, "tone.wav"))
    for k, vv in maxabs_and_tone(r).items():
        print(f"  {k:20s} {vv}")

    print("""
== how to read this ==
  tone present in first100ms during the FIRST zeros stream, absent on the
  second, and the self-check shows the tone strongly
      -> DIGITAL: a firmware module emits 1 kHz in its first frames. Find the
         module with uninitialised state (DSP filter state, generator phase,
         gain tables) and zero it at init.
  mirror silent in ALL windows and the self-check still shows the tone
      -> the words on I2S are silence: the tone is made by the codec when
         AudioHwConfig re-pokes it at stream open. Fix the bring-up order in
         csd_AudioHwConfig / Mute / UnMute, not the DSP.
  self-check silent
      -> the mirror is not wired on this build: INCONCLUSIVE, do not conclude.""")
    return 0

if __name__ == "__main__":
    sys.exit(main())
