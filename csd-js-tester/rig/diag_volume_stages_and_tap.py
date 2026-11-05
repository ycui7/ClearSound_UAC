#!/usr/bin/env python3
"""Debug the INSTRUMENT, not the verdict (owner directive 2026-11-05).

Two questions that decide whether cases 09 and 06/07 are firmware bugs or
harness faults:

 Q1  Are the two "Playback Volume" controls independent per channel? If driving
     numid 5 alone drops BOTH channels, the pair is not independent and the
     suite's habit of setting both is what produced the "volume applied twice"
     result -- an artefact of the tester, not the DAC.
 Q2  Does the loopback tap arm and carry samples? Query its index map and its
     frame counter during a real stream, with retries, because the diag path is
     known to go quiet under load and a single dropped reply is not a verdict.
"""
import os, re, subprocess, sys, time
import numpy as np

REPO = os.path.expanduser("~/sw-audio-sc/sw_usb_audio/clearsound_uac")
CLIENT = os.path.join(REPO, "csd-js-tester", "csd_diag.py")
PY = sys.executable
DAC, MIC = 3, 5


def sh(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def scpi(cmd, tries=3, timeout=10):
    """Retry: the diag responder is starved while the audio path is hot, and a
    one-shot timeout was already mistaken for 'command does not exist'."""
    last = ""
    for i in range(tries):
        rc, out = sh([PY, CLIENT, "--backend", "rawmidi", "--timeout", str(timeout), cmd],
                     timeout=timeout + 30)
        last = out.strip().strip('"')
        if last and "TIMEOUT" not in last.upper():
            return last, i
        time.sleep(0.4)
    return last, tries


def cget(nid):
    return sh(["amixer", "-c", str(DAC), "cget", "numid=%d" % nid])[1]


def cset(nid, v):
    sh(["amixer", "-c", str(DAC), "cset", "numid=%d" % nid, str(v)])


def tone(path, secs=2.0, freq=1000.0, ch=None, dbfs=-6.0):
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


def peak(mic=MIC, secs=2, freq=1000.0):
    cap = "/tmp/probe_cap.wav"
    if os.path.exists(cap):
        os.remove(cap)
    sh(["arecord", "-q", "-D", "hw:%d,0" % mic, "-f", "S24_3LE", "-r", "48000",
        "-c", "2", "-d", str(secs), "-t", "wav", cap], timeout=90)
    if not os.path.exists(cap):
        return None
    import wave
    with wave.open(cap, "rb") as w:
        n = w.getnframes()
        d = np.frombuffer(w.readframes(n), dtype=np.uint8)
    raw = d.reshape(n, 2, 3).astype(np.uint32)
    v = (raw[:, :, 0] | (raw[:, :, 1] << 8) | (raw[:, :, 2] << 16)).astype(np.int32)
    v = np.where(v & 0x800000, v - (1 << 24), v).astype(np.float64) / (1 << 23)
    out = {}
    for c in (0, 1):
        sp = np.abs(np.fft.rfft(v[:, c] * np.hanning(len(v))))
        fr = np.fft.rfftfreq(len(v), 1 / 48000.0)
        k = int(np.argmin(np.abs(fr - freq)))
        out[c] = 20 * np.log10(sp[max(1, k - 2):k + 3].max() / (len(v) / 4) + 1e-12)
    return out


def measure_with_tone(path, secs=2):
    p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-i", path,
                          "-f", "alsa", "-ac", "2", "plughw:%d,0" % DAC],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    pk = peak(secs=secs)
    p.terminate()
    try:
        p.wait(timeout=10)
    except Exception:
        p.kill()
    time.sleep(0.3)
    return pk


print("=" * 70)
print("Q1  ARE THE TWO Playback Volume CONTROLS INDEPENDENT?")
print("=" * 70)
for nid in (5, 6):
    print("numid %d cget: %s" % (nid, " / ".join(l.strip() for l in cget(nid).splitlines()
                                                 if "values=" in l or "dBminmax" in l)))
# drive each control alone, measure both channels
for label, a, b in (("both at 192", 192, 192), ("only numid5=96", 96, 192),
                    ("only numid6=96", 192, 96), ("both at 96", 96, 96)):
    cset(5, a); cset(6, b)
    time.sleep(0.3)
    pk = measure_with_tone(tone("/tmp/probe_q1.wav", 3.0))
    print("%-16s numid5=%3d numid6=%3d ->  L %7.1f  R %7.1f dBFS" % (
        label, a, b, pk[0], pk[1]) if pk else "%-16s capture failed" % label)
cset(5, 192); cset(6, 192)

print()
print("=" * 70)
print("Q2  DOES THE LOOPBACK TAP ARM AND CARRY SAMPLES?")
print("=" * 70)
for q in ("DEBUG:LOOPback?", "DEBUG:LOOPback:INDEX?"):
    txt, tries = scpi(q)
    print("%-22s -> %-40s (%d tries)" % (q, txt if txt else "<no reply>", tries))
print("arm   ->", repr(scpi("DEBUG:LOOPback 1")[0]))
print("clear ->", repr(scpi("DEBUG:LOOPback:CLEAR")[0]))
print("state after arm/clear ->", repr(scpi("DEBUG:LOOPback?")[0]))

# query the counter DURING a stream: the counter only moves while samples flow
p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
                      "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=8",
                      "-ac", "2", "-f", "alsa", "plughw:%d,0" % DAC],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.5)
for i in range(3):
    txt, tries = scpi("DEBUG:LOOPback?", tries=2, timeout=8)
    print("  during stream %d: LOOPback? -> %-24s (%d tries)" % (i, txt if txt else "<no reply>", tries))
    time.sleep(1.0)
p.terminate()
try:
    p.wait(timeout=10)
except Exception:
    p.kill()
print("state after stream ->", repr(scpi("DEBUG:LOOPback?")[0]))

print()
print("=" * 70)
print("Q3  WHAT DOES THE CAPTURE STREAM CARRY WHILE AUDIO PLAYS? (case 11)")
print("=" * 70)
idle = peak(secs=2, freq=1000.0)
p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
                      "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=6",
                      "-ac", "2", "-f", "alsa", "plughw:%d,0" % DAC],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(0.7)
busy = peak(secs=2, freq=1000.0)
p.terminate()
try:
    p.wait(timeout=10)
except Exception:
    p.kill()
print("capture at the 1 kHz bin: idle L %.1f R %.1f  |  while playing L %.1f R %.1f dBFS"
      % (idle[0], idle[1], busy[0], busy[1]))
print("  -> if the capture bin rises by the played level, the capture MIRRORS the")
print("     playback stream (a diagnostic loop), which is not a microphone path.")
