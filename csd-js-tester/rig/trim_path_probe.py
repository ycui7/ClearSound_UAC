#!/usr/bin/env python3
"""Where does host volume actually live? (decides where trim must go)

The register probe found the ES9219Q volume registers frozen at their BOOT
values (0F=0, 10=0, 03=64) through a VERIFIED -12 dB host SET_CUR. Two
explanations fit, and they lead to different fixes:

  (1) host volume is applied in the mixer (XUA_OUT_VOLUME_IN_MIXER=1 forces
      lib_xua's ep0 to send SET_MIX_OUT_VOL and its AudioHwRemote_Volume_Update
      branch is never compiled), so the codec registers legitimately never
      move -- trim must ride the mixer's per-channel multiplier;
  (2) host volume commands never arrive at all, so BOTH the mixer multiplier
      and the codec path are dead, and the -12 dB we verified is an ALSA
      fiction -- trim would have nowhere to live until that is fixed.

Discriminator: DEBUG:GENerator:VOLume? -> cmds,last_index,last_multiplier
counts SET_MIX_OUT_VOL at its DESTINATION (mixer.xc's own handler). This is
the instrument the repo added precisely because amixer cget, raw control
transfers, and the xrun console all failed to answer the question.

  cmds advances + multiplier sane (0x20000000-ish = 0 dB)  -> (1)
  cmds stays 0 across a verified SET_CUR                    -> (2)

Also prints the codec register triple alongside, so the two instruments are
compared in the same breath rather than across runs.

Usage: python trim_path_probe.py      (debug build: uses DEBUG: commands)
"""
import base64, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = HERE.rsplit("/rig", 1)[0]
PY = os.path.join(APP, ".venv", "bin", "python")
sys.path.insert(0, HERE)
import ears_matrix as EM

def scpi(cmd, timeout=10):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return ((rr.stdout or "") + (rr.stderr or "")).strip()

def nums(out):
    return [int(t) for t in out.replace("\r", " ").replace(",", " ").split()]

def reg_read(addrs):
    tok = base64.b64encode(bytes(addrs)).decode()
    out = scpi(f'REGister:READ? "{tok}"').strip().strip('"')
    try:
        return list(base64.b64decode(out, validate=True))
    except Exception:
        return None

def card(*pats):
    for line in open("/proc/asound/cards"):
        m = re.search(r"^\s*(\d+)\s*\[", line)
        if m and any(p in line.lower() for p in pats):
            return m.group(1)
    sys.exit("cards missing")

XMOS = card("xmos", "clearsound")
TMP = os.environ.get("TMPDIR", "/tmp")
REGS = (0x0F, 0x10, 0x03)

def snap(tag):
    v = nums(scpi("DEBUG:GENerator:VOLume?"))
    r = reg_read(REGS)
    if len(v) >= 3:
        cmds, idx, mult = v[0], v[1], v[2]
        # multiplier is Q2.29 per the mixer; 0 dB is ~0x20000000 * 4 (the
        # mixer's own scaling) -- report raw and let the numbers speak.
        db = "n/a"
        try:
            import math
            if mult > 0:
                db = f"{20*math.log10(mult/2**31):+.2f} dB(rel 0 dBFS)"
            else:
                db = "-inf"
        except Exception:
            pass
        print(f"  [{tag}] SET_MIX_OUT_VOL cmds={cmds} last_idx={idx} "
              f"mult={mult} (0x{mult:08x}) {db}")
    else:
        print(f"  [{tag}] DEBUG:GENerator:VOLume? -> {v!r}  (debug build required)")
    print(f"          codec regs 0F={r[0]:3d} 10={r[1]:3d} 03={r[2]:3d}" if r else
          f"          codec regs unreadable")
    return (v if len(v) >= 3 else None), r

print(scpi("*IDN?"))

print("\n== 1. baseline snapshot ==")
base_v, base_r = snap("baseline")

print("\n== 2. verified host SET_CUR: 0 dB -> -12 dB -> 0 dB ==")
EM.set_volume(int(XMOS), EM.VOL_0DB - 48, EM.VOL_0DB - 48, TMP)
mid_v, mid_r = snap("after -12 dB")
EM.set_volume(int(XMOS), EM.VOL_0DB, EM.VOL_0DB, TMP)
end_v, end_r = snap("after back to 0 dB")

print("\n== 3. verdict ==")
if base_v is None or end_v is None:
    print("  VOID -- DEBUG:GENerator:VOLume? unavailable. Run on a DEBUG build;")
    print("  an unproved leg is void, never evidence of either branch.")
    sys.exit(2)
d_cmds = end_v[0] - base_v[0]
print(f"  SET_MIX_OUT_VOL advanced by {d_cmds} across 2 SET_CURs "
      f"(2 channels x 2 changes = 4 expected if ep0 sends per channel)")
if d_cmds == 0:
    print("  -> branch (2): host volume commands never reached the mixer.")
    print("     The -12 dB ALSA verified is a codec-driver cache, not the device.")
    print("     Trim has no working volume path to ride; fix that first.")
else:
    print("  -> branch (1): host volume IS the mixer multiplier. The frozen")
    print("     codec registers are correct behaviour, and trim must be applied")
    print("     to the mixer's per-channel out multiplier, not to regs 15/16.")
sys.exit(0)
