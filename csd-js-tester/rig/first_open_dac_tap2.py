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
  (b) the words are silence and the codec makes the tone itself when
      AudioHwConfig re-pokes it at stream open (-> fix the bring-up order).

Sensor: the board's OWN capture stream. With -DCSD_DSP_DIAG_LOOP the audio
thread assigns sampsFromAudioToUsb[0..1] = sampsFromUsbToAudio[0..1] AFTER
csd_dsp_frame(), i.e. exactly the words it hands to I2S (csd_dsp.xc, the
DIAG_LOOP block -- it runs last, so it also wins over DIAG_BUMP). No capsule,
no cable, no room: a tone in these samples is content the firmware made.

Analysis is a 100 ms profile across the whole capture, so no launch-time
alignment is needed -- the profile itself says whether energy is confined to
the start (a start event) or spans the capture (streamed content).

Self-check: streaming a real 1 kHz tone must light the same profile, else the
mirror is not wired and a quiet result proves nothing.
"""
from __future__ import annotations
import os, subprocess, sys, tempfile, time, wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ears_matrix as EM

SLOT = 4800          # 100 ms at 48 kHz


def tap(dac, secs, stim, path):
    """Record the board's own capture (its mirror of the I2S-bound words)
    while `stim` is played through playback. stim=None: no stream at all."""
    EM.kill_orphan_arecords()
    cap = subprocess.Popen(
        ["arecord", "-D", f"hw:{dac},0", "-f", "S32_LE", "-r", str(EM.RATE),
         "-c", "2", "-d", str(secs), "-t", "wav", path],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if stim is not None:
        time.sleep(0.5)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-i", stim,
             "-f", "alsa", "-ac", "2", f"plughw:{dac},0"],
            capture_output=True, text=True)
    rc = cap.wait()
    err = (cap.stderr.read().decode(errors="replace") if cap.stderr else "").strip()
    if rc != 0 or not os.path.exists(path) or os.path.getsize(path) < 1000:
        raise SystemExit(f"mirror capture failed (rc={rc}): {err[:200]}")
    return path


def load(path):
    import numpy as np
    w = wave.open(path); n = w.getnframes(); sw = w.getsampwidth()
    raw = w.readframes(n); w.close()
    if sw == 4:
        return np.frombuffer(raw, dtype="<i4").reshape(-1, 2), float(1 << 31)
    if sw == 3:
        a = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.uint32)
        v = (a[:, 0] | (a[:, 1] << 8) | (a[:, 2] << 16)).astype(np.int32)
        v[v >= 1 << 23] -= 1 << 24
        return v.reshape(-1, 2), float(1 << 23)
    raise SystemExit(f"unexpected capture width {sw}")


def profile(path, nslots=30, verbose=False):
    """Per-100 ms slot: peak |sample| and the loudest 1 kHz bin, both channels."""
    import numpy as np
    x, full = load(path)
    k = int(round(EM.TONE_HZ / EM.RATE * 1024))
    win = np.hanning(1024)
    rows = []
    for s in range(min(nslots, len(x) // SLOT)):
        seg = x[s * SLOT:(s + 1) * SLOT]
        pk = -999.0
        for c in range(2):
            ch = seg[:, c].astype(np.float64)
            m = np.abs(np.fft.rfft(ch[:1024] * win))
            pk = max(pk, float(m[max(1, k - 2):k + 3].max()))
        rows.append((s, int(np.max(np.abs(seg))),
                     float(20 * np.log10(pk / full + 1e-12))))
    if verbose:
        for s, mx, db in rows[:12]:
            print(f"    slot {s:2d} ({s * 100:4d} ms)  peak|s| {mx:11d}  1kHz {db:+7.1f} dBFS")
    return rows


def summary(tag, rows):
    import numpy as np
    hot = [r[0] for r in rows if r[2] > -80]
    worst = max((r[2] for r in rows), default=-999.0)
    mx = max((r[1] for r in rows), default=0)
    print(f"  {tag}: peak|s| {mx} ({20 * np.log10(mx / 2**31 + 1e-12):.1f} dBFS)"
          f"  slots with 1 kHz > -80 dBFS: {hot}  worst {worst:.1f} dBFS")
    return worst


def main():
    out = subprocess.run(["./.venv/bin/python", "csd_diag.py", "--backend", "rawmidi",
                          "--timeout", "8", "*IDN?"], cwd=os.path.dirname(HERE),
                         capture_output=True, text=True, timeout=40).stdout.strip()
    if "ClearSound" not in out:
        print(f"ABORT: firmware is not ours ({out!r})"); return 1
    print(f"LIVE BUILD: {out}")
    print("artifact must carry -DCSD_DSP_DIAG_LOOP (the gate build does)")

    ears = EM.find_ears_card(None); dac = EM.find_dac_card(None)
    tmp = tempfile.mkdtemp(prefix="first_open_dac_")
    EM.ensure_capture_switch(ears)
    EM.set_volume(dac, EM.VOL_0DB, EM.VOL_0DB, tmp)

    # Arm the board's OWN capture path. Without these the mirror reads pure
    # silence and the self-check "passes" as a nil result -- measured: the
    # first run of this rig reported the tone at -144 dBFS because the mixer
    # never routed capture (rig/mixer_mirror_probe.py has the working list).
    def numid_for(card, name):
        o = subprocess.run(["amixer", "-c", str(card), "controls"],
                           capture_output=True, text=True).stdout
        return [l.split(",", 1)[0].split("=")[1] for l in o.splitlines()
                if name in l and l.startswith("numid=")]
    armed = []
    for nm in ("Extension Unit Switch", "Mix 2 Capture Switch"):
        for nid in numid_for(dac, nm):
            subprocess.run(["amixer", "-q", "-c", str(dac), "cset",
                            f"numid={nid}", "--", "on"], capture_output=True)
            armed.append(f"{nm}@{nid}")
    for nid in numid_for(dac, "Mix 2 Playback Volume"):
        for v in ("0,0", "192,192"):
            subprocess.run(["amixer", "-q", "-c", str(dac), "cset",
                            f"numid={nid}", "--", v], capture_output=True)
            time.sleep(0.1)
    print("mirror armed:", armed or "NOTHING FOUND -- results will be void")

    zeros = os.path.join(tmp, "zeros.wav"); EM.write_tone24(zeros, 3.5, 0.0, 0.0)
    tone = os.path.join(tmp, "tone.wav");  EM.write_tone24(tone, 3.5, 0.8, 0.8)

    print("\n== FIRST stream this board has ever opened (host zeros) ==")
    w1 = summary("first ", profile(tap(dac, 3, zeros, os.path.join(tmp, "first.wav")),
                                   verbose=True))
    print("\n== SECOND zeros stream (control: first-open only?) ==")
    w2 = summary("second", profile(tap(dac, 3, zeros, os.path.join(tmp, "second.wav"))))
    print("\n== SELF-CHECK: real 1 kHz tone streamed; mirror must light up ==")
    wt = summary("tone  ", profile(tap(dac, 3, tone, os.path.join(tmp, "tone.wav"))))

    print("\n== verdict ==")
    if wt < -60:
        print(f"  INCONCLUSIVE: the self-check tone only reached {wt:.1f} dBFS, so "
              "the DIAG_LOOP mirror is not carrying the I2S-bound words on this "
              "build. Nothing above can be concluded.")
        return 2
    if w1 > -60:
        print(f"  DIGITAL: the mirror carries 1 kHz at {w1:.1f} dBFS during the "
              "FIRST zeros stream. A firmware module emits content in its first "
              "frames -- find the uninitialised state and zero it.")
    elif w2 > -60:
        print(f"  DIGITAL but not first-open specific: tone present on the SECOND "
              f"stream too ({w2:.1f} dBFS). Re-read the acoustic numbers.")
    else:
        print(f"  ANALOG: the I2S-bound words sit at the noise floor ({w1:.1f} dBFS) "
              "while the mic hears +12 dB. The codec makes the tone itself when "
              "AudioHwConfig re-pokes it at stream open -- fix the bring-up order "
              "in csd_AudioHwConfig / Mute / UnMute, not the DSP.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
