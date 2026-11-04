#!/usr/bin/env python3
"""Master-Trim register verification, directive 2026-11-04.

Reads ES9219Q regs 17..20 through the PROVEN quoted REGister:READ? path
(unquoted base64 answers nothing -- automute_ab.py documents the trap; the
bit_perfect_noise/one_shot_block codec gate fell into it too and is fixed
separately), and compares against an EXACT port of the device-side math:

  csd_trim_counts_to_mult_q29(counts): 0 -> 0x20000000; else binary
  exponentiation of STEP=529200047 in Q2.29 with csd_trim_long_mul =
  (a*b)>>29; common = min(L,R) counts, clamped <=0.
  q31 = q29 >> 2, capped 0x7FFFFFFF. Byte split default MSB-first at reg17.

If measured != expected the first suspicion is byte order (rig leg 5),
second is that these regs do not echo writes. The script cannot settle
which the chip means acoustically -- only the null-depth rig can; this
proves only that what we intend reaches the register file.
"""
import subprocess, base64, sys, time

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"
UNITY = 0x20000000
STEP  = 529200047
MASK29 = (1 << 32) - 1

def long_mul(a, b):          # device: (a*b) >> 29, 64-bit product
    return ((a * b) >> 29) & MASK29

def counts_to_mult_q29(counts):
    if counts >= 0:
        return UNITY
    steps = min(-counts, 256)
    r, b, e = UNITY, STEP, steps
    while e:
        if e & 1:
            r = long_mul(r, b)
        e >>= 1
        if e:
            b = long_mul(b, b)
    return r

def expected_q31(left, right):
    common = min(left, right)
    if common > 0:
        common = 0
    q31 = counts_to_mult_q29(common) >> 2
    return min(q31, 0x7FFFFFFF)

def scpi(cmd, tries=3):
    # A setter's reply can arrive after csd_diag.py's window when the
    # tile[0] server is mid-journal: the first sweep saw VOLume:TRIM?
    # answer EMPTY right after setters while the WRITE had landed. Empty
    # on a QUERY is therefore retried, and every setter is followed by a
    # settle + confirm read -- never trust a bare setter reply.
    for _ in range(tries):
        o = subprocess.run([".venv/bin/python", "csd_diag.py", "--backend", "rawmidi",
                            "--timeout", "8", cmd], capture_output=True, text=True,
                           cwd=APP, timeout=15).stdout.strip().splitlines()
        r = o[0].strip().strip('"') if o else ""
        if r or "?" not in cmd:
            return r
        time.sleep(0.4)
    return ""

def read_regs(addrs):
    tok = base64.b64encode(bytes(addrs)).decode()
    r = scpi(f'REGister:READ? "{tok}"')
    try:
        return base64.b64decode(r + "=" * (-len(r) % 4))
    except Exception:
        return None

fails = 0
def check(name, got, want):
    global fails
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got} want {want}")
    if not ok:
        fails += 1

TRIM = [0x11, 0x12, 0x13, 0x14]
print("== device math port self-check (must match firmware constants) ==")
print(f"  q29(0)   = 0x{counts_to_mult_q29(0):08X}  (unity 0x20000000)")
print(f"  q29(-96) = 0x{counts_to_mult_q29(-96):08X}")
q = expected_q31(-96, 0)
print(f"  q31(L-96,R0) = 0x{q:08X}")

print("== boot state ==")
st = scpi("VOLume:TRIM?")
print(f"  VOLume:TRIM? -> {st}")
b = read_regs(TRIM)
if b is None:
    sys.exit("ABORT: quoted REGister:READ? failed at boot")
print(f"  regs17..20 -> 0x{int.from_bytes(b,'big'):08X}")
l, r = (int(x) for x in st.split(","))
check("boot matches mailbox", b.hex(), expected_q31(l, r).to_bytes(4, "big").hex())

for (lv, rv) in [(-96, 0), (0, -256), (-32, -128), (0, 0)]:
    print(f"== set LEFT {lv} RIGHT {rv} ==")
    scpi(f"VOLume:TRIM:LEFT {lv}")
    scpi(f"VOLume:TRIM:RIGHT {rv}")
    time.sleep(0.5)                      # settle: journal + reg writes
    st = scpi("VOLume:TRIM?")
    if st != f"{lv},{rv}":
        print(f"  RETRY: mailbox read {st!r} != {lv},{rv}")
        scpi(f"VOLume:TRIM:LEFT {lv}"); scpi(f"VOLume:TRIM:RIGHT {rv}")
        time.sleep(0.8); st = scpi("VOLume:TRIM?")
    print(f"  VOLume:TRIM? -> {st}")
    want = expected_q31(lv, rv)
    got = read_regs(TRIM)
    if got is None:
        print("  FAIL: read returned nothing"); fails += 1; continue
    g = int.from_bytes(got, "big")
    print(f"  regs17..20 -> 0x{g:08X}   (expected q31 0x{want:08X})")
    print(f"    bytes: got {got.hex()}  want {want.to_bytes(4,'big').hex()}  "
          f"swapped-want {want.to_bytes(4,'little')[::-1].hex()}")
    check(f"set({lv},{rv})", g, want)

print()
print("ALL PASS" if fails == 0 else f"{fails} FAILED -- byte-order leg still open")
sys.exit(1 if fails else 0)
