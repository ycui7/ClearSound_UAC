#!/usr/bin/env python3
"""CONCURRENT fork test: EARS mic AND the five firmware nodes, same stream.

State of the case-A investigation (all on cold loads):

  factory image   mic delta +1.0 dB
  production      mic delta +5.8 dB
  mixer (4 mix)   mic delta +11.7 dB     digital nodes: 0x96969696 prefill
  mixer + prefill fix (zero underflow word for PCM)  +24 dB
  passthrough (MIXER=1 MAX_MIX_COUNT=0, official 316_mc shape)  +24 dB
      ...with rig/dac_node_probe2.py proving the FIVE nodes (host, postmix,
      volume, dac, token) carry ZERO words for a full first stream.

The digital node probe and the acoustic rig have never measured the SAME
stream, so the fork (codec makes it vs the stream differs) is still open.
This rig closes it: for the FIRST stream after load, capture E.A.R.S. while
the firmware simultaneously records the five-node peaks of that exact
stream.

  mic tone AND nodes zero      -> the words reaching I2S are silence: the
                                  codec/analog produces the tone; stop
                                  looking in the mixer, look at codec state
                                  (clocks, mute history) at stream open.
  mic tone AND nodes carry it  -> the instrument missed it (fix instrument).
  mic quiet during this stream -> the ears_matrix stream is different from
                                  this one (rate/format/volume path); the
                                  difference between the two rigs IS the
                                  variable; bisect the rigs, not firmware.
"""
from __future__ import annotations
import math, os, subprocess, sys, time, wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ears_matrix as EM

APP = os.path.dirname(HERE)
PY = f"{APP}/.venv/bin/python"
FS = float(1 << 31)
NODES = ("host", "postmix", "volume", "dac", "token", "predsp", "postdsp")


def scpi(cmd, timeout=8):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()


def nodes():
    s = scpi("MIXer:DACPeak?")
    try:
        v = [int(x) for x in s.split(",")]
    except ValueError:
        return None
    if len(v) != 33:
        return None
    out = {n: tuple(v[4 * i:4 * i + 4]) for i, n in enumerate(NODES)}
    out["tail"] = tuple(v[28:])
    return out


def dbfs(v):
    return max(20 * math.log10(v / FS + 1e-12), -240.0)


def main():
    idn = scpi("*IDN?")
    if "ClearSound" not in idn:
        print(f"ABORT: firmware is not ours ({idn!r})"); return 1
    print(f"LIVE BUILD: {idn}")
    ears = EM.find_ears_card(None)
    dac = EM.find_dac_card(None)
    tmp = EM.tempfile.mkdtemp(prefix="concurrent_")
    EM.ensure_capture_switch(ears)
    EM.set_volume(dac, EM.VOL_0DB, EM.VOL_0DB, tmp)

    Z = os.path.join(tmp, "z.wav"); EM.write_tone24(Z, 4.0, 0.0, 0.0)
    T = os.path.join(tmp, "t.wav"); EM.write_tone24(T, 4.0, 0.8, 0.8)

    def i2s():
        s = scpi("DIAG:I2S?")
        try:
            v = [int(x) for x in s.split(",")]
            return dict(zip(("frames", "bad", "hi", "lo", "firstbad"), v)) if len(v) == 5 else None
        except ValueError:
            return None

    def leg(tag, wav, first=False):
        if first:
            scpi("MIXer:DACPeak:CLEAR")
            scpi("DIAG:I2S:CLEAR")
        cap_wav = os.path.join(tmp, f"{tag}.wav")
        EM.kill_orphan_arecords()
        cap = subprocess.Popen(
            ["arecord", "-D", f"hw:{ears},0", "-f", "S24_3LE", "-r", "48000",
             "-c", "2", "-d", "4", "-t", "wav", cap_wav],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        time.sleep(0.5)
        pr = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-i", wav,
             "-f", "alsa", "-ac", "2", f"plughw:{dac},0"],
            capture_output=True, text=True, timeout=60)
        cap.wait()
        if pr.returncode:
            print(f"  PLAYBACK-FAIL {pr.stderr.strip()[:150]}")
        r = EM.analyse(cap_wav, tones=(EM.TONE_HZ,))
        n = nodes()
        i = i2s()
        mx = max(max(n[x][0], n[x][1]) for x in NODES) if n else -1
        i2s_txt = (f"i2s frames {i['frames']} bad {i['bad']} "
                   f"delta range {i['lo']}..{i['hi']} firstbad@{i['firstbad']}"
                   if i else "i2s ERR")
        print(f"  {tag:16s} mic 1kHz L {r['L'].tone_db[1000.0]:+6.1f} "
              f"R {r['R'].tone_db[1000.0]:+6.1f} dBFS   "
              f"nodes peak {dbfs(mx) if mx >= 0 else 'ERR':>8} dBFS  "
              f"nzL={n['tail'][0] if n else '?'}  {i2s_txt}")
        return {c: r[c].tone_db[1000.0] for c in "LR"}, mx

    print("\n== A0: mic only, no stream ever ==")
    EM.kill_orphan_arecords()
    a0w = os.path.join(tmp, "a0.wav")
    subprocess.run(["arecord", "-D", f"hw:{ears},0", "-f", "S24_3LE",
                    "-r", "48000", "-c", "2", "-d", "4", "-t", "wav", a0w],
                   check=True)
    a0 = EM.analyse(a0w, tones=(EM.TONE_HZ,))
    print(f"  mic 1kHz L {a0['L'].tone_db[1000.0]:+6.1f} R {a0['R'].tone_db[1000.0]:+6.1f}")

    print("\n== FIRST stream after load: zeros, mic + nodes SIMULTANEOUS ==")
    z, mx = leg("first-zeros", Z, first=True)
    dl = max(z[c] - a0[c].tone_db[1000.0] for c in "LR")
    print(f"  delta vs A0: {dl:+.1f} dB")

    print("\n== SELF-CHECK: real tone, mic + nodes ==")
    t, mxt = leg("tone", T)

    print("\n== verdict ==")
    tone_mic = max(t[c] for c in "LR")
    if tone_mic - max(a0[c].tone_db[1000.0] for c in "LR") < 20.0:
        print("  MIC SELF-CHECK FAIL (tone not heard) -- mic dead, no verdict.")
        return 2
    if dbfs(mxt) < -6.0:
        print("  INSTRUMENT SELF-CHECK FAIL: nodes never saw the tone.")
        return 2
    if dl >= 6.0:
        if dbfs(mx) < -60.0:
            print(f"  FORK CLOSED -> CODEC/ANALOG: mic heard {dl:+.1f} dB over the"
                  " floor on a stream whose DAC-bound words the firmware itself"
                  " recorded as SILENCE (nodes peak "
                  f"{dbfs(mx):.0f} dBFS). The mixer cannot be the source; the"
                  " tone is generated at/after the codec during stream open."
                  " Next: diff codec REGISTER STATE between factory and our"
                  " builds right after the first stream opens.")
            return 1
        print(f"  FORK CLOSED -> DIGITAL: nodes peak {dbfs(mx):+.1f} dBFS"
              " on the zeros stream -- content is coming from firmware; follow"
              " the node ordering.")
        return 1
    print(f"  mic quiet ({dl:+.1f} dB) during THIS zeros stream, while"
          " ears_matrix measures +24 dB on ITS zeros stream. The two streams"
          " differ -- bisect the rigs (rate/format/volume path/start order)."
          " Neither codec nor mixer is implicated by this result.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
