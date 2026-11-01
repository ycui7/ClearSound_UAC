#!/usr/bin/env python3
"""SCPI release-gate check (TODO items 5+6).

Run inside a loaded-image lifetime (run_xe.sh) with MODE=debug or MODE=release:

  production commands must ALWAYS answer (not -113):
      *IDN?, DIAG:DSP:PRESet?, MIXer:GAIN?, GENerator:STATe?
  debug-group commands answer numbers in a DEBUG build and -113 in RELEASE:
      DEBUG:MIXer:FRAMES?, DEBUG:I2S?, DEBUG:GENerator:COUNts?
  retired names must answer -113 in BOTH builds (no shadow of the old table):
      MIXer:FRAMES?, DIAG:I2S?, GENerator:COUNts?

Usage: MODE=debug|release python scpi_gate_check.py
"""
import os, subprocess, sys

APP = os.path.dirname(os.path.abspath(__file__)).rsplit("/rig", 1)[0]
PY = os.path.join(APP, ".venv", "bin", "python")
MODE = os.environ.get("MODE", "")
if MODE not in ("debug", "release"):
    sys.exit("MODE=debug|release required")

def scpi(cmd, timeout=10):
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return ((rr.stdout or "") + (rr.stderr or "")).strip()

def answers(out):
    """A real reply: not an SCPI error and not a transport timeout."""
    return bool(out) and "-113" not in out and "TIMEOUT" not in out.upper()

PROD = ["*IDN?", "DIAG:DSP:PRESet?", "MIXer:GAIN?", "GENerator:STATe?"]
DBG  = ["DEBUG:MIXer:FRAMES?", "DEBUG:I2S?", "DEBUG:GENerator:COUNts?"]
OLD  = ["MIXer:FRAMES?", "DIAG:I2S?", "GENerator:COUNts?"]

ok = True
def need(cond, msg):
    global ok
    print(f"  {'OK ' if cond else 'BAD'} {msg}")
    ok = ok and cond

idn = scpi("*IDN?")
print(f"  IDN: {idn}")
need("ClearSound" in idn, "board answered *IDN? (transport alive)")

for c in PROD:
    need(answers(scpi(c)), f"production {c!r} answers in {MODE} build")
for c in DBG:
    if MODE == "debug":
        need(answers(scpi(c)), f"debug {c!r} answers in debug build")
    else:
        out = scpi(c)
        need("-113" in out, f"debug {c!r} rejected -113 in release build")
for c in OLD:
    out = scpi(c)
    need("-113" in out, f"retired {c!r} answers -113 in {MODE} build")

print("  SCPI GATE: PASS" if ok else "  SCPI GATE: FAIL")
sys.exit(0 if ok else 1)
