#!/usr/bin/env python3
"""Is audio alive after a DFU update? Deliberately NO SCPI, no xrun, no
monitor reset: playback is host->device ALSA and the measurement is the
E.A.R.S mic, which is the path a customer takes after a field DFU.

Why no SCPI: after a DFU-exit reboot the MIDI-scpi responder can be silent
while the audio function enumerates normally (observed twice; a backtrace
showed every task, csd_diag_midi included, parked in its normal select loop).
An SCPI-dependent rig would report 'board dead' and conflate the two.

Legs, one boot:
  PLAIN         play 1 kHz, measure the mic. True post-DFU state.
  AFTER-REBIND  only if PLAIN fails: unbind/rebind snd-usb-audio, the
                software equivalent of an unplug/replug. Separates 'firmware
                wedged' from 'host driver kept a stale card instance after an
                abrupt reboot with no USB disconnect'.
The generator is off at boot (armed only over SCPI, which nothing here
touches), so any 1 kHz at the mic came from the host stream through the DAC.

Exit 0 = audio works (plain or after rebind); 2 = dead both ways.
"""
import math, os, re, subprocess, sys, time, wave

import numpy as np

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
RATE, TONE_HZ, SECS = 48000, 1000.0, 3
HOST_AMP = 0.4
TONE_MIN = 30.0          # dB above the noise floor that counts as a live tone
PLAYBACK_RC = {}         # filled by capture(): ffmpeg exit code per leg
TMP = os.environ.get("TMPDIR", "/tmp")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ears_matrix as EM   # proven switch/volume helpers -- do not paraphrase


def card(*pats):
    for line in open("/proc/asound/cards"):
        m = re.search(r"^\s*(\d+)\s*\[", line)
        if m and any(p in line.lower() for p in pats):
            return m.group(1)
    return None


def make_tone(path, amp):
    FULL = 2 ** 23 - 1
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
        buf = bytearray()
        for i in range(int(RATE * 10)):
            v = int(round(amp * FULL * math.sin(2 * math.pi * TONE_HZ * i / RATE)))
            u = max(-FULL, min(FULL - 1, v)) & 0xFFFFFF
            buf += bytes((u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF)) * 2
        w.writeframes(bytes(buf))


def capture(level_hz, tag, play=True):
    """arecord the mic; return FFT magnitude at level_hz for L and R, dBFS,
    from the tail window. play=False leaves the DAC silent, which is the ONLY
    way to get a real noise floor -- an earlier version always streamed the
    tone and then compared tone against tone, so a WORKING board read
    'NO-TONE' (the control run caught it)."""
    subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
    subprocess.run(["pkill", "-f", "postdfu_tone.wav"], capture_output=True)
    time.sleep(0.3)
    raw = f"{TMP}/postdfu_{tag}.raw"
    cap = subprocess.Popen(
        ["arecord", "-q", "-D", f"hw:{MIC},0", "-f", "S24_3LE", "-r", str(RATE),
         "-c", "2", "-d", str(SECS), "-t", "raw", raw],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ff = None
    if play:
        time.sleep(0.7)
        fflog = f"{TMP}/postdfu_{tag}_ff.log"
        ff = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error",
                               "-t", str(SECS - 0.5), "-i", TONE_WAV, "-f", "alsa",
                               "-ac", "2", f"plughw:{XMOS},0"],
                              stdout=subprocess.DEVNULL, stderr=open(fflog, "w"))
    cap.wait(timeout=SECS + 8)
    if ff is not None:
        ff.wait(timeout=15)
        PLAYBACK_RC[tag] = ff.returncode
        if ff.returncode:
            print(f"    playback FAILED rc={ff.returncode}: "
                  f"{open(fflog).read().strip()[:160]}")
    else:
        PLAYBACK_RC[tag] = 0
    b = np.frombuffer(open(raw, "rb").read(), dtype=np.uint8)
    if b.size < 3 * 1024:
        return None
    b = b[:(b.size // 3) * 3].reshape(-1, 3)
    v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
         (b[:, 2].astype(np.int32) << 16))
    v = np.where(v & 0x800000, v - (1 << 24), v)
    out = {}
    tail = int(RATE * 1.3)
    for name, s in (("L", v[0::2][-tail:]), ("R", v[1::2][-tail:])):
        x = s.astype(np.float64)
        # normalise by S24_3LE full scale, as case_a_mute_ab.py does, so the
        # numbers are comparable with the acoustic gate (a live tone lands
        # near -18 dBFS there, a dead channel near -95).
        spec = np.abs(np.fft.rfft(x - x.mean())) * 2.0 / x.size / float(2 ** 23)
        k = int(round(level_hz * x.size / RATE))
        out[name] = 20 * np.log10(max(spec[k - 2:k + 3].max(), 1e-12))
    return out


def rebind_driver():
    """Software unplug/replug for our USB audio function."""
    dev = None
    for d in subprocess.run(["ls", "/sys/bus/usb/devices/"], capture_output=True,
                            text=True).stdout.split():
        p = f"/sys/bus/usb/devices/{d}/"
        try:
            if open(p + "idVendor").read().strip() == "20b1" and \
               open(p + "idProduct").read().strip() == "0018":
                dev = d
        except OSError:
            continue
    if dev is None:
        print("  rebind: sysfs path not found")
        return False
    drv = "/sys/bus/usb/drivers/snd-usb-audio"
    for op in ("unbind", "bind"):
        try:
            with open(f"{drv}/{op}", "w") as f:
                f.write(dev)
        except OSError as e:
            print(f"  rebind {op} failed: {e}")
            return False
    time.sleep(4)
    return True


XMOS, MIC = card("xmos", "clearsound"), card("e36db", "e.a.r.s", "36db")
if XMOS is None or MIC is None:
    sys.exit(f"cards missing (dac={XMOS} mic={MIC}) -- is the DAC enumerated?")
print(f"cards: dac=card{XMOS} mic=card{MIC}")

EM.ensure_capture_switch(int(MIC))
EM.set_volume(int(XMOS), EM.VOL_0DB, EM.VOL_0DB, TMP)

TONE_WAV = os.path.join(TMP, "postdfu_tone.wav")
make_tone(TONE_WAV, HOST_AMP)

floor = capture(TONE_HZ, "floor_off", play=False)   # DAC silent: true floor
if floor is None:
    sys.exit("mic capture failed -- rig fault, not a firmware verdict")
print(f"  mic floor @1k        : L {floor['L']:+6.1f}  R {floor['R']:+6.1f} dBFS")

for stage in ("PLAIN", "AFTER-REBIND"):
    if stage == "AFTER-REBIND":
        if not rebind_driver():
            print("  rebind leg unavailable (needs root?)")
            break
        XMOS = card("xmos", "clearsound") or XMOS
        MIC = card("e36db", "e.a.r.s", "36db") or MIC
        EM.ensure_capture_switch(int(MIC))
        EM.set_volume(int(XMOS), EM.VOL_0DB, EM.VOL_0DB, TMP)
    r = capture(TONE_HZ, stage.lower())
    if r is None:
        print(f"  {stage:13s}: capture failed")
        continue
    dl, dr = r["L"] - floor["L"], r["R"] - floor["R"]
    ok = dl > TONE_MIN and dr > TONE_MIN
    print(f"  {stage:13s}: L {r['L']:+6.1f} (+{dl:5.1f})  "
          f"R {r['R']:+6.1f} (+{dr:5.1f}) dB  {'TONE-PRESENT' if ok else 'NO-TONE'}")
    if ok and PLAYBACK_RC.get(stage) == 0:
        print(f"AUDIO-OK ({stage}) -- the DFU-written image plays audio")
        sys.exit(0)
    if ok and PLAYBACK_RC.get(stage) not in (0, None):
        print(f"  {stage}: tone present but playback reported rc="
              f"{PLAYBACK_RC[stage]} -- not counted as OK")

print("AUDIO-DEAD: no tone before or after a driver rebind")
sys.exit(2)
