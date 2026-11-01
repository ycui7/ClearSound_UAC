#!/usr/bin/env python3
"""L/R trim PERSISTENCE proof (TODO item 3). Owner directive 2026-11-01.

Driven by rig/trim_persist_run.sh, which invokes this twice against one build:
  PERSIST_MODE=arm     set a trim, journal it, record the expectation
  PERSIST_MODE=verify  after a FRESH firmware load, check it came back

WHY TWO PROCESSES AND NOT ONE
Persistence means "survives the firmware restarting". One process cannot
observe that: the value it set is still in RAM when it reads it back, so a
read-back in the same life proves nothing. xrun loads the .xe into RAM and
starts it, so killing xrun and starting it again restarts the firmware the way
a power cycle does for anything held in RAM -- every static and the mailbox
word return to their reset values. A non-zero trim that comes back after that
can only have come from flash.

WHY THE STATUS COUNTERS ARE MANDATORY, NOT BONUS
The documented reset default IS 0 dB / 0 dB. So the naive rig -- set, reboot,
read back -- passes when nothing worked: if the journal never landed, if the
board has no data partition, or if the store task died, the read-back returns
0/0 and so does the "nothing was ever set" case. That is a vacuous pass, the
failure mode this project has been bitten by twice. Every leg therefore also
asserts on VOLume:TRIM:STATus?:
    partition_bytes > 0   a data partition EXISTS (board flashed with --data)
    load_status == 1      a VALID record was parsed (2 = none, 3 = flash error)
    seq                   the journal's own sequence number
The load_status condition is what the reset default cannot fake.

WHY AN ACOUSTIC LEG ON TOP
SCPI proves the value came back from flash; it does not prove the DAC plays
it. The acoustic legs measure the loaded pair and then a forced 0/0 pair in
the SAME life, so the delta is self-calibrating against room, mic gain and
host volume -- and a record that loads into the mailbox but never reaches the
mixer multiply cannot pass it.

EXIT CODES: 0 pass, 1 fail, 3 armed-and-needs-reload (arm mode only).
"""
import os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = HERE.rsplit("/rig", 1)[0]
PY = os.path.join(APP, ".venv", "bin", "python")
sys.path.insert(0, HERE)
import ears_matrix as EM

MODE = os.environ.get("PERSIST_MODE", "verify")
EXP = os.path.join(HERE, ".trim_expect")

RATE, TONE_HZ, SECS = 48000, 1000.0, 3
GEN_AMP = 1073741824                 # half-scale Q31, as the other rigs
PEAK_FLOOR = -45.0                   # a real tone lands far above this
TMP = os.environ.get("TMPDIR", "/tmp")


def scpi(cmd, timeout=10):
    """Reply text from stdout AND stderr: csd_diag prints some replies on
    stderr with rc=1, and reading only stdout turns a good reply into an
    empty string that parses as a pass."""
    rr = subprocess.run([PY, "csd_diag.py", "--backend", "rawmidi",
                         "--timeout", str(timeout), cmd],
                        capture_output=True, text=True, cwd=APP,
                        timeout=timeout + 10)
    return ((rr.stdout or "") + (rr.stderr or "")).strip()


def nums(out):
    return [int(t) for t in out.replace("\r", " ").replace(",", " ").split()]


def card(*pats):
    for line in open("/proc/asound/cards"):
        m = re.search(r"^\s*(\d+)\s*\[", line)
        if m and any(p in line.lower() for p in pats):
            return m.group(1)
    sys.exit("cards missing: " + " ".join(pats))


FAILS = []
def ck(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def status(label):
    v = nums(scpi("VOLume:TRIM:STATus?"))
    if len(v) < 7:
        print(f"  FAIL {label}: no VOLume:TRIM:STATus? reply ({v!r}). Is the "
              f"persistence build loaded? An older build has no such command.")
        FAILS.append(f"{label}: status unavailable")
        return None
    part, load, save, seq, err, bootn, saven = v[:7]
    print(f"  {label}: partition={part} load={load} save={save} seq={seq} "
          f"err={err} boots={bootn} saves={saven}")
    return dict(part=part, load=load, save=save, seq=seq, err=err,
                boots=bootn, saves=saven)


def set_trim(which, counts):
    """Set one channel and report whether the device REJECTED it.

    Empty error queue replies 0,"No error" -- which parses to [0], a TRUTHY
    list. `if nums(...)` therefore passes when nothing was rejected, which is
    exactly the vacuous pass this project keeps getting bitten by. The test is
    on the numeric error CODE being non-zero, never on the list being
    non-empty. Note a valid set can also report non-zero now: a failed
    journal pushes an error on purpose (set-but-not-saved must not look like
    saved), so callers that expect acceptance must check for zero too.
    """
    scpi(f"VOLume:TRIM:{which} {counts}")
    v = nums(scpi("SYST:ERR?"))
    rejected = bool(v) and v[0] != 0
    return rejected, v


def drain_errors():
    """Empty the queue so a stale error cannot satisfy a later assertion."""
    for _ in range(20):
        v = nums(scpi("SYST:ERR?"))
        if not v or v[0] == 0:
            return
    print("  (error queue would not drain)")


# ---- acoustic harness (the gate's own helpers, unchanged) ---------------
XMOS = MIC = None
Z = f"{TMP}/tp_zeros.wav"


def audio_init():
    global XMOS, MIC, Z
    XMOS, MIC = card("xmos", "clearsound"), card("e36db", "e.a.r.s", "36db")
    EM.ensure_capture_switch(int(MIC))
    if not os.path.exists(Z):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i",
                        f"anullsrc=r={RATE}:cl=stereo", "-t", str(SECS), Z],
                       check=True)


def measure(tag):
    """One capture with the CURRENT trim. Returns (L dBFS, R dBFS) at TONE_HZ,
    or None if no tone reached the mic (void, not a pass)."""
    scpi("GENerator:OFF")
    EM.set_volume(int(XMOS), EM.VOL_0DB, EM.VOL_0DB, TMP)
    scpi(f"GENerator:SINe {RATE},{int(TONE_HZ * 1000)},{GEN_AMP}")
    out = f"{TMP}/tp_{tag}.wav"
    EM.record(int(XMOS), int(MIC), SECS, Z, out)
    scpi("GENerator:OFF")
    res = EM.analyse(out, tones=(TONE_HZ,))
    l, r = res["L"].tone_db[TONE_HZ], res["R"].tone_db[TONE_HZ]
    print(f"  [{tag}] L={l:+6.2f} R={r:+6.2f} dBFS   "
          f"floor L={res['L'].floor:+.1f} R={res['R'].floor:+.1f}")
    if max(l, r) < PEAK_FLOOR:
        print("  (no tone at the mic -- rig is not hearing the DAC; VOID)")
        return None
    return l, r


# ============================== ARM ======================================
if MODE == "arm":
    print("== ARM: contract legs, then journal a known non-zero pair ==")
    drain_errors()
    for bad in ("1", "-257", "abc", "0.5"):
        rejected, errs = set_trim("LEFT", bad)
        ck(rejected, f"rejected VOLume:TRIM:LEFT {bad} (error code {errs})")
    drain_errors()

    # A VALID set must report acceptance -- including no journal failure.
    rej, errs = set_trim("LEFT", 0)
    ck(not rej, f"valid VOLume:TRIM:LEFT 0 accepted (error code {errs})")
    set_trim("RIGHT", 0)
    drain_errors()
    st = status("cleared")
    if st:
        ck(st["part"] > 0,
           f"data partition present ({st['part']} B). If 0 the board was "
           f"flashed WITHOUT --data and persistence cannot work at all -- "
           f"re-flash with --data before believing anything below.")

    L, R = -64, -128              # -8 dB / -16 dB: non-zero, so 0/0 can't fake it
    rej_l, errs_l = set_trim("LEFT", L)
    ck(not rej_l, f"valid VOLume:TRIM:LEFT {L} accepted AND journalled "
                  f"(a failed journal pushes an error on purpose; code {errs_l})")
    set_trim("RIGHT", R)
    ck(nums(scpi("VOLume:TRIM?")) == [L, R],
       f"read-back in the same life == [{L},{R}]")

    # No sleep: the journal is synchronous through the codec-server proxy, so
    # by the time VOLume:TRIM:* returned, the value was in flash or the status
    # said why not. A sleep here would hide a non-blocking regression.
    st = status("after sets")
    if not st:
        sys.exit(1)
    ck(st["save"] == 1, f"journal write reported ok (save={st['save']} err={st['err']})")
    ck(st["saves"] >= 1, f"save_count advanced ({st['saves']})")
    if st["save"] != 1 or st["part"] == 0:
        print("\nARM ABORTED: nothing journalled, so a reload would only")
        print("measure the 0 dB/0 dB reset default. NOT tested, NOT passed.")
        sys.exit(1)

    # Aggregate the contract legs into the exit code. Without this, arm
    # reports rc=3 ("armed") after printing FAILs, phase 2 never re-checks
    # the contract, and the final verdict silently omits every contract
    # failure. A leg that fails here means the rig should stop, not proceed.
    if FAILS:
        print("\nARM FAILED contract legs:")
        for m in FAILS:
            print("  BAD", m)
        sys.exit(1)

    with open(EXP, "w") as f:
        f.write(f"{L} {R} {st['seq']} {st['saves']}\n")
    print(f"\nARMED L={L} R={R} seq={st['seq']} -- reload the firmware, then")
    print("run with PERSIST_MODE=verify.")
    sys.exit(3)

# ============================ VERIFY =====================================
print("== VERIFY: after a fresh firmware load ==")
if not os.path.exists(EXP):
    print("  FAIL no expectation file -- run PERSIST_MODE=arm first")
    sys.exit(1)
L, R, seq0, saves0 = [int(x) for x in open(EXP).read().split()]
print(f"  expecting L={L} R={R} (seq was {seq0}, saves was {saves0})")

echo = nums(scpi("VOLume:TRIM?"))
st = status("post-reload")
ck(echo == [L, R], f"read-back after reload {echo} == [{L},{R}]")
if st:
    ck(st["part"] > 0, f"data partition present ({st['part']} B)")
    ck(st["load"] == 1,
       f"load_status==1 (2 = 'no valid record', 3 = 'flash error'; either way "
       f"the read-back above was the 0 dB/0 dB DEFAULT, not persistence)")
    ck(st["seq"] == seq0, f"journal seq survived the reload ({st['seq']} == {seq0})")
    ck(st["err"] == 0, f"no flash error (err={st['err']})")
    ck(st["boots"] >= 1, f"boot load ran on this life (boots={st['boots']})")
    ck(st["saves"] == saves0,
       f"save_count did NOT grow across the reload ({st['saves']} == {saves0}) "
       f"-- the load path must never journal")
else:
    FAILS.append("no status after reload: cannot tell persistence from default")

# Acoustic: loaded pair vs forced 0/0, same life, so room/mic/host-volume all
# cancel. Done last: the 0/0 leg journals, which is expected and restored.
try:
    audio_init()
    print(scpi("*IDN?"))
    a = measure("loaded")
    set_trim("LEFT", 0); set_trim("RIGHT", 0)
    time.sleep(0.6)
    b = measure("zero")
    if a and b:
        for ch, got, idx in (("L", L, 0), ("R", R, 1)):
            want = got / 8.0                       # counts -> dB
            delta = a[idx] - b[idx]
            if got == 0:
                ck(abs(delta) < 1.5, f"{ch} untrimmed: delta {delta:+.2f} dB ~ 0")
            else:
                ck(abs(delta - want) < 1.5,
                   f"{ch} measured {delta:+.2f} dB, expected {want:+.2f} dB "
                   f"from the RELOADED trim -- the flash value drives the DAC")
    else:
        FAILS.append("acoustic legs returned no tone: VOID, not a pass")
    # put the factory pair back so the board is left as verified
    set_trim("LEFT", L); set_trim("RIGHT", R)
except Exception as e:
    print(f"  FAIL acoustic legs did not run: {type(e).__name__}: {e}")
    FAILS.append("acoustic legs did not run")

os.remove(EXP)                        # verified: never re-verify a stale file
print("\n== VERDICT ==")
if FAILS:
    for m in FAILS:
        print("  BAD", m)
    sys.exit(1)
print("  PERSISTENCE PASS: a non-zero trim pair survived a firmware reload;")
print("  the store reports a parsed VALID record on a present data partition;")
print("  and the measured acoustic delta matches the reloaded values. The")
print("  0 dB/0 dB reset default is excluded by load_status==1 and by the")
print("  armed values.")
sys.exit(0)
