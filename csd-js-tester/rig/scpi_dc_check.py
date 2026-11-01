#!/usr/bin/env python3
"""GENerator:DC check (TODO item 2).

Host plays zeros; the generator arms a DC wave mid-stream; the words handed
to the DAC (DEBUG:MIXer:DACPeak? node 3) must be EXACTLY the armed amp on
both channels: peak == word == amp. The mic cannot witness DC (AC-coupled
codec input, DC bin meaningless after the EARS codec), so this is measured
digitally at the hand-off — the only honest instrument for a constant.

Geometry mirrors mix2_sanity (the proven gates) deliberately:
 - ALSA 'Mix 2 Playback Volume' pinned to 0 dB FIRST. The control's device
   default after re-enumeration is 0 counts = our MIN_VOLUME = mute, which
   legitimately zeroes every DAC-bound word; the first version of this rig
   omitted the set and honestly measured silence.
 - ffmpeg streams zeros, DC armed mid-stream.
 - COUNts read BEFORE and AFTER so emission evidence is windowed to this
   leg, not boot-cumulative (the counters never reset by design).

Usage: python scpi_dc_check.py [amp]   (default amp = 0x40000000 = -6 dBFS)
"""
import os, re, subprocess, sys, time

APP = os.path.dirname(os.path.abspath(__file__)).rsplit("/rig", 1)[0]
PY = os.path.join(APP, ".venv", "bin", "python")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ears_matrix as EM                      # same helper the gates use
RATE, SECS = 48000, 3
TMP = os.environ.get("TMPDIR", "/tmp")
AMP = int(sys.argv[1], 0) if len(sys.argv) > 1 else 0x40000000

def scpi(cmd, timeout=10):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return (rr.stdout or "").strip()

def nums(out):
    # SCPI numeric replies are COMMA-separated on one line ("1,2,3\r\n") --
    # splitting on whitespace alone makes every reply parse empty.
    return [int(t) for t in out.replace("\r", " ").replace(",", " ").split()]

def card(*pats):
    for line in open("/proc/asound/cards"):
        m = re.search(r"^\s*(\d+)\s*\[", line)
        if m and any(p in line.lower() for p in pats):
            return m.group(1)
    sys.exit("cards missing")

XMOS = card("xmos", "clearsound")
EM.set_volume(int(XMOS), EM.VOL_0DB, EM.VOL_0DB, TMP)
print(scpi("*IDN?"))


def leg(wave_cmd, tag):
    """One zeros-stream leg with wave_cmd armed mid-stream. Returns
    (frames_delta, emit_delta, [33 DACPeak values])."""
    scpi("GENerator:OFF")
    scpi("DEBUG:MIXer:DACPeak:CLEAR")
    f0 = nums(scpi("DEBUG:MIXer:FRAMES?"))
    c0 = nums(scpi("DEBUG:GENerator:COUNts?"))
    ff = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error",
                           "-t", str(SECS), "-i", wav, "-f", "alsa",
                           "-ac", "2", f"plughw:{XMOS},0"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.6)
    arm = scpi(wave_cmd)
    print(f"  [{tag}] arm reply: {arm!r}")
    ff.wait(timeout=SECS + 10)
    time.sleep(0.4)
    f1 = nums(scpi("DEBUG:MIXer:FRAMES?"))
    c1 = nums(scpi("DEBUG:GENerator:COUNts?"))
    v = nums(scpi("DEBUG:MIXer:DACPeak?"))
    fd = (f1[0] - f0[0]) if (f1 and f0) else -1
    ed = (c1[0] - c0[0]) if (c1 and c0) else -1
    print(f"  [{tag}] mixer FRAMES delta={fd}  gen emit delta={ed}")
    return v


def report(v, tag):
    print(f"  [{tag}] node0(host) peakL={v[0]} peakR={v[1]}")
    print(f"  [{tag}] node1(postmix) peakL={v[4]} peakR={v[5]} wordL=0x{v[6] & 0xFFFFFFFF:08X}")
    print(f"  [{tag}] node2(vol) peakL={v[8]} peakR={v[9]}")
    print(f"  [{tag}] node3(DAC) peakL={v[12]} peakR={v[13]} "
          f"wordL=0x{v[14] & 0xFFFFFFFF:08X} wordR=0x{v[15] & 0xFFFFFFFF:08X}")

wav = f"{TMP}/dc_zeros.wav"
subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"anullsrc=r={RATE}:cl=stereo",
                "-t", str(SECS), wav], check=True)

scpi("GENerator:OFF")
scpi("DEBUG:MIXer:DACPeak:CLEAR")
c0 = nums(scpi("DEBUG:GENerator:COUNts?"))

# A/B with the identical instrument: a 1 kHz SINE leg first. If sine lands a
# peak at node3 and DC does not, the DC case in csd_gen_sample is the fault.
# If sine ALSO reads zero at node3, the instrument/geometry is the fault and
# the DC result means nothing. Same rig, same leg, only the wave differs.
vs = leg(f"GENerator:SINe {RATE},1000000,{AMP}", "sine")
report(vs, "sine")
print()
vd = leg(f"GENerator:DC {RATE},{AMP},0", "dc")
report(vd, "dc")

v = vd
c1 = nums(scpi("DEBUG:GENerator:COUNts?"))

emit = (c1[0] - c0[0]) if (c1 and c0) else -1
sup = (c1[1] - c0[1], c1[2] - c0[2]) if len(c1) > 2 and len(c0) > 2 else ("?", "?")
print(f"  COUNts delta over the leg: emit={emit} sup_dsd={sup[0]} sup_dop={sup[1]}")
if len(v) < 16:
    print(f"  BAD short DACPeak reply: {v}")
    sys.exit(1)
# node 0 (host receive) = words 0..3; node 3 (DAC hand-off) = words 12..15:
# peakL, peakR, wordL, wordR
hl, hr = v[0], v[1]
pl, pr, wl, wr = v[12], v[13], v[14], v[15]
print(f"  armed amp = {AMP} (0x{AMP:08X})")
print(f"  node0(host) peakL={hl} peakR={hr}")
print(f"  node3(DAC)  peakL={pl} peakR={pr} wordL=0x{wl & 0xFFFFFFFF:08X} "
      f"wordR=0x{wr & 0xFFFFFFFF:08X}")
st = nums(scpi("GENerator:STATe?"))
print(f"  STATe mode={st[0]} fs={st[1]} amp={st[4]} (mode 6 = DC)")

ok = True
def need(cond, msg):
    global ok
    print(f"  {'OK ' if cond else 'BAD'} {msg}")
    ok = ok and cond

need(st[0] == 6, "STATe reports DC mode 6")
need(emit > 1000, f"generator emitted during the leg ({emit} frames)")
need(hl == 0 and hr == 0, "host words stayed zero (content is the wave alone)")
need(pl == AMP and pr == AMP, "both channel peaks equal armed amp")
need((wl & 0xFFFFFFFF) == AMP and (wr & 0xFFFFFFFF) == AMP,
     "both raw words ARE the armed amp (constant, no oscillator)")
scpi("GENerator:OFF")
print("  DC GATE: PASS" if ok else "  DC GATE: FAIL")
sys.exit(0 if ok else 1)
