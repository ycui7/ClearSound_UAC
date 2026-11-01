#!/usr/bin/env python3
"""1 kHz FFT, codec UNMUTED, plotted.

Use when a transducer (headphone / speaker) has just been attached and the
question is "what does the DAC actually put out now". Single leg, no mute
comparison -- for the mute figure use rig/case_a_mute_ab.py.

Capture geometry is copied from rig/case_a_mute_ab.py:capture_1k (the proven
path): mic opens, 0.7 s later the SAME sine file starts, FFT window is the tail
after the stream has settled. Same stimulus, same geometry, so numbers are
comparable to the gate rows in release/RELEASE-*.md.

Two things this rig does NOT do:
  - it does not use csd_diag.py for SCPI. That client opens and closes the
    rawmidi substream per query and loses every reply after the first (device
    parses and F7-terminates all of them; the loss is client-side). A rig that
    needs several queries would silently proceed on stale/absent answers, so
    every SCPI call here goes over ONE held fd and waits for the F7 terminator.
  - it does not assume the tone is there. No tone -> rc 2 INVALID, because a
    mute-vs-nothing or FFT-of-silence plot looks exactly like a working
    measurement (that was the flaw in the first version of case_a_mute_ab.py,
    which streamed zeros in both legs and measured only the room).

Levels are referenced to full scale = 2**23 for S24_3LE. Forgetting that
normalisation is how one rig once reported "+121 dBFS".
"""
import base64, math, os, re, select, subprocess, sys, time, wave

import numpy as np

# ears_matrix imports scipy.io.wavfile at module scope, and scipy is absent
# from the one venv that also has matplotlib. Shim the submodule instead of
# installing a 60 MB package or dropping the plot: this rig reads WAV frames
# with the stdlib `wave` module and never calls scipy, so an ImportError on
# attribute access is the honest failure -- if someone later routes a real
# scipy call through here it raises rather than silently misbehaving.
try:
    import scipy.io.wavfile  # noqa: F401
except ModuleNotFoundError:
    import types
    _s = types.ModuleType("scipy"); _si = types.ModuleType("scipy.io")
    _sw = types.ModuleType("scipy.io.wavfile")
    def _unavailable(*_a, **_k):
        raise RuntimeError("scipy is not installed in this interpreter; "
                           "rig/hp_fft_1k.py does not use it, but this call "
                           "would have needed it.")
    _sw.read = _sw.write = _unavailable
    _si.wavfile = _sw; _s.io = _si
    sys.modules.setdefault("scipy", _s)
    sys.modules.setdefault("scipy.io", _si)
    sys.modules.setdefault("scipy.io.wavfile", _sw)
    print("note: scipy absent -- shimmed (unused by this rig)", flush=True)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
FW_SRC = ("/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac"
          "/src/extensions/csd_diag_midi.xc")
RATE, TONE_HZ, SECS = 48000, 1000.0, 3
HOST_AMP = 0.4                     # same stimulus level as the acoustic gate
REG = 0x07                         # codec system mute: bit0 1 = muted
FS = 2 ** 23                       # full scale for S24_3LE (24-bit signed)
TMP = os.environ.get("TMPDIR", "/tmp")
OUT = os.environ.get("HP_FFT_PNG", "/home/ycui/workspace/clearsound-hp-fft.png")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ears_matrix as EM


# ---- SCPI over ONE held rawmidi fd ------------------------------------------
def sysex_id():
    txt = open(FW_SRC, errors="ignore").read()
    m = re.search(r"#define\s+CSD_SYSEX_ID\s+(0x[0-9A-Fa-f]+)", txt)
    if not m:
        sys.exit("CSD_SYSEX_ID not found in csd_diag_midi.xc")
    return int(m.group(1), 16)


def card(*pats):
    for line in open("/proc/asound/cards"):
        m = re.search(r"^\s*(\d+)\s*\[", line)
        if m and any(p in line.lower() for p in pats):
            return m.group(1)
    sys.exit("cards missing")


class Diag:
    def __init__(self):
        cards = open("/proc/asound/cards").read()
        m = re.search(r"^\s*(\d+)\s*\[.*\]:.*(ClearSound|XMOS)", cards, re.M | re.I)
        if not m:
            sys.exit("no ClearSound/XMOS card -- is the DAC enumerated?")
        self.path = f"/dev/snd/midiC{m.group(1)}D0"
        self.sid = sysex_id()
        self.f = os.open(self.path, os.O_RDWR)

    def close(self):
        try:
            os.close(self.f)
        except OSError:
            pass

    def ask(self, cmd, secs=8.0):
        body = (bytes([0xF0, self.sid])
                + bytes(b & 0x7F for b in (cmd + "\r\n").encode()) + b"\xF7")
        # clear stragglers, then send, then read to the F7 terminator
        self._drain(0.2)
        off, deadline = 0, time.time() + 2.0
        while off < len(body):
            n = os.write(self.f, body[off:])
            if n <= 0:
                if time.time() > deadline:
                    raise RuntimeError(f"write stalled at {off}/{len(body)}")
                time.sleep(0.001)
                continue
            off += n
        return self._drain(secs)

    def _drain(self, secs):
        buf, t0 = b"", time.time()
        while time.time() - t0 < secs:
            r, _, _ = select.select([self.f], [], [], 0.2)
            if not r:
                continue
            try:
                buf += os.read(self.f, 4096)
            except BlockingIOError:
                continue
            if b"\xf7" in buf:
                return buf
        return buf                       # caller checks for the terminator

    def ok(self, out):
        return b"\xf7" in out

    def payload(self, out):
        """Text inside F0 <id> ... F7, trimmed of CR/LF and quotes.

        csd_diag.py used to do this step for us; a rig that reads the raw node
        must, or every parse silently yields None and the rig proceeds on
        nothing. Empty string means no well-formed frame -- the caller must
        treat that as a failure, not as a value."""
        if not self.ok(out):
            return ""
        i = out.find(b"\xf0")
        if i < 0:
            return ""
        j = out.rfind(b"\xf7")
        return out[i + 2:j].decode("latin-1").strip().strip('"').strip()

    def reg_rd(self, a):
        out = self.ask(f'REGister:READ? "{base64.b64encode(bytes([a])).decode()}"')
        tok = self.payload(out)
        try:
            return list(base64.b64decode(tok, validate=True))[0]
        except Exception:
            # Print what actually came back. Returning a bare None is how a
            # parse bug gets mistaken for a dead codec; the raw frame makes it
            # a one-line diagnosis.
            print(f"  reg_rd(0x{a:02X}) unparseable: payload={tok!r} raw={out[:60]!r}",
                  flush=True)
            return None

    def reg_wr(self, a, v):
        tok = base64.b64encode(bytes([a, v])).decode()
        self.ask(f'REGister:WRITE "{tok}"')
        return self.reg_rd(a) == v        # the rig trusts a read-back, never a write


# ---- stimulus ----------------------------------------------------------------
def make_tone(path, amp):
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(RATE)
        buf = bytearray()
        for i in range(int(RATE * 10)):
            v = int(round(amp * (FS - 1) * math.sin(2 * math.pi * TONE_HZ * i / RATE)))
            u = max(-(FS), min(FS - 1, v)) & 0xFFFFFF
            buf += bytes((u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF)) * 2
        w.writeframes(bytes(buf))


def welch(x, nseg=4096):
    """Welch PSD without scipy (absent from this venv). Returns (freqs, dBFS/Hz)
    referenced to full scale, so a full-scale sine at nseg bins lands at the
    expected dBFS and the plot is readable in absolute terms."""
    n = min(nseg, x.size)
    hop = n // 2
    win = np.hanning(n)
    acc = np.zeros(n // 2 + 1)
    cnt = 0
    for s in range(0, max(1, x.size - n), hop):
        seg = x[s:s + n]
        if seg.size < n:
            break
        sp = np.abs(np.fft.rfft((seg - seg.mean()) * win)) ** 2
        acc += sp
        cnt += 1
    if cnt == 0:
        # No segments: return a floor at/below the axis bottom. -200 would be
        # a dB value leaking into a linear axis and would render as nothing.
        return np.fft.rfftfreq(n, 1 / RATE), np.full(n // 2 + 1, 1e-18)
    acc = acc / cnt
    scale = (n / 2) * (np.sum(win ** 2)) * (FS ** 2)
    psd = acc / scale
    # LINEAR, not dB: callers plot this with semilogy against an axis labelled
    # PSD (FS^2/Hz). Returning 10*log10(psd) here put ~-114..-26 dB values on a
    # log axis, matplotlib silently drops non-positive data, and the rendered
    # chart was empty apart from the axvline -- a plot that LOOKS like a
    # measurement and carries none. Floor clamp must stay below the axis
    # bottom (1e-14) so the noise floor is visible rather than clipped.
    psd = np.maximum(psd, 1e-18)
    return np.fft.rfftfreq(n, 1 / RATE), psd


def tone_dbfs(x):
    spec = np.abs(np.fft.rfft(x - x.mean())) * 2.0 / x.size
    k = int(round(TONE_HZ * x.size / RATE))
    band = spec[max(1, k - 3):k + 4]
    return 20 * np.log10(max(band.max(), 1e-12) / FS), int(np.argmax(band) + max(1, k - 3))


def main():
    XMOS, MIC = card("xmos", "clearsound"), card("e36db", "e.a.r.s", "36db")
    print(f"DAC card {XMOS}, mic card {MIC}", flush=True)

    after = None      # assigned inside try; finally must survive its absence
    d = Diag()
    try:
        idn = d.ask("*IDN?")
        if not d.ok(idn):
            sys.exit("no *IDN? reply -- the DAC is enumerated but not answering."
                     " Do NOT proceed: every number below would be meaningless.")
        stamp = d.payload(idn)
        print(f"LIVE BUILD: {stamp}", flush=True)

        EM.ensure_capture_switch(int(MIC))
        EM.set_volume(int(XMOS), EM.VOL_0DB, EM.VOL_0DB, TMP)
        d.ask("GENerator:OFF")           # the sine comes from the HOST stream

        before = d.reg_rd(REG)
        print(f"reg 7 before = {before}", flush=True)
        if not d.reg_wr(REG, 0x00):
            sys.exit(f"could not set reg 7 UNMUTED (read-back gave "
                     f"{d.reg_rd(REG)}) -- refusing to plot an unknown state")

        T = os.path.join(TMP, "hp_tone.wav")
        make_tone(T, HOST_AMP)

        subprocess.run(["pkill", "-x", "arecord"], capture_output=True)
        time.sleep(0.3)
        raw = os.path.join(TMP, "hp_unmute.raw")
        cap = subprocess.Popen(
            ["arecord", "-q", "-D", f"hw:{MIC},0", "-f", "S24_3LE", "-r",
             str(RATE), "-c", "2", "-d", str(SECS), "-t", "raw", raw],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.7)
        ff = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error",
                               "-t", str(SECS - 0.5), "-i", T, "-f", "alsa",
                               "-ac", "2", f"plughw:{XMOS},0"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.8)
        if d.reg_rd(REG) != 0x00:        # our write must be the LAST one
            print("  NOTE: firmware re-wrote reg 7 during bring-up; re-asserting",
                  flush=True)
            d.reg_wr(REG, 0x00)
        cap.wait(timeout=SECS + 8)
        ff.wait(timeout=8)
        after = d.reg_rd(REG)

        b = np.frombuffer(open(raw, "rb").read(), dtype=np.uint8)
        b = b[:(b.size // 3) * 3].reshape(-1, 3)
        v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8)
             | (b[:, 2].astype(np.int32) << 16))
        v = np.where(v & 0x800000, v - (1 << 24), v)
    finally:
        # Restore the mute state we found; never leave the codec muted. The
        # `after is not None` guard matters: if the measurement leg died
        # before the read-back, there is no state to reason about, and touching
        # registers on the way out of an unrelated failure would corrupt the
        # very evidence needed to diagnose it.
        if after is not None and after != 0x00:
            d.reg_wr(REG, 0x00)
        d.close()

    tail = int(RATE * 1.5)               # FFT the settled tail, tone running
    sig = {"L": v[0::2][-tail:].astype(np.float64),
           "R": v[1::2][-tail:].astype(np.float64)}
    lvl = {}
    for ch, x in sig.items():
        db, k = tone_dbfs(x)
        lvl[ch] = db
        print(f"  {ch}: 1 kHz = {db:+6.1f} dBFS  (bin {k}, "
              f"{k * RATE / x.size:7.1f} Hz)", flush=True)

    au = min(lvl.values())
    if au < -40.0:
        print(f"\nINVALID -- no tone while UNMUTED ({au:+.1f} dBFS). The stimulus"
              "\nnever reached the DAC, or the transducer is not coupled to the"
              "\nmic. A plot of this would look like a measurement and is not one.")
        return 2

    fig, ax = plt.subplots(2, 2, figsize=(13, 7.5))
    for j, ch in enumerate(("L", "R")):
        x = sig[ch]
        fr, psd = welch(x)
        # y limits FIRST: an axvline added while a log axis still autoscales
        # warns 'Data has no positive values' and drops the line, which would
        # silently remove the 1 kHz marker -- the point of the chart.
        ax[0, j].set_ylim(1e-14, 1e-2)
        ax[0, j].semilogy(fr, psd, lw=0.8, color="tab:blue")
        ax[0, j].axvline(TONE_HZ, color="red", ls="--", lw=1, ymin=0.0, ymax=1.0,
                         label="1 kHz")
        ax[0, j].set_xlim(0, 20000)
        ax[0, j].set_xlabel("Hz"); ax[0, j].set_ylabel("PSD  (FS²/Hz)")
        ax[0, j].set_title(f"{ch}: 0–20 kHz   1 kHz = {lvl[ch]:+.1f} dBFS")
        ax[0, j].grid(True, which="both", alpha=0.3); ax[0, j].legend(fontsize=8)

        ax[1, j].set_ylim(1e-14, 1e-2)
        ax[1, j].semilogy(fr, psd, lw=1.0, color="tab:blue")
        ax[1, j].axvline(TONE_HZ, color="red", ls="--", lw=1, ymin=0.0, ymax=1.0)
        ax[1, j].set_xlim(600, 1500)
        ax[1, j].set_xlabel("Hz"); ax[1, j].set_ylabel("PSD  (FS²/Hz)")
        ax[1, j].set_title(f"{ch}: 1 kHz zoom (600–1500 Hz)")
        ax[1, j].grid(True, which="both", alpha=0.3)
    fig.suptitle(f"ClearSound DAC — 1 kHz unmuted, host sine @ "
                 f"{20*math.log10(HOST_AMP):.1f} dBFS, 48 kHz S24_3LE\n"
                 f"build {stamp}   mic E.A.R.S card {MIC}   "
                 f"reg7=0x00 (unmuted), DAC volume {EM.VOL_0DB}",
                 fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fig.savefig(OUT, dpi=125)
    print(f"\nPNG: {OUT}")
    print(f"HP-FFT-OK L {lvl['L']:+.1f} dBFS / R {lvl['R']:+.1f} dBFS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
