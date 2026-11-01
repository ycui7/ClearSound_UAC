#!/usr/bin/env python3
"""Is the post-DFU-exit MIDI death an endpoint HALT?

If the MIDI bulk OUT endpoint is stalled, host data never arrives and the
diag task sits forever in select on midi_get_ack_or_data -- which is exactly
the backtrace we took (task healthy, parked at csd_diag_midi.xc:291), while
audio plays fine because its endpoints are unaffected.

Reads GET_STATUS(ENDPOINT) per bulk endpoint (halted = bit 0). With --clear it
also issues CLEAR_FEATURE(ENDPOINT_HALT) on the MIDI endpoints and reports
what happened, which is the discriminator: SCPI returning after that means
device-side endpoint state, fixable at boot; still dead means something
deeper in the firmware's data path.
"""
import subprocess, sys, time
import usb.core

BM_IN_STD_EP = 0x82          # device->host, standard, ENDPOINT recipient
BM_OUT_STD_EP = 0x02
GET_STATUS, CLEAR_FEATURE = 0x00, 0x01
EP_FEATURE_HALT = 0
MIDI_IF = 0x07

dev = usb.core.find(idVendor=0x20b1, idProduct=0x0018)
if dev is None:
    sys.exit("runtime PID (20b1:0018) not present")
cfg = dev.get_active_configuration()

do_clear = "--clear" in sys.argv
rows = []
for i in cfg:
    for e in i:                      # an Interface iterates its endpoints
        addr = e.bEndpointAddress
        out = not (addr & 0x80)
        kind = {0: "ctrl", 1: "iso", 2: "bulk", 3: "intr"}.get(
            e.bmAttributes & 0x03, "?")
        if kind not in ("bulk", "intr"):
            continue
        try:
            st = dev.ctrl_transfer(BM_IN_STD_EP, GET_STATUS, EP_FEATURE_HALT,
                                   addr, 1, timeout=3_000)[0]
            halted = bool(st & 1)
        except usb.core.USBError as ex:
            st, halted = f"err({ex})", None
        is_midi = (i.bInterfaceClass == MIDI_IF)
        rows.append((i.bInterfaceNumber, addr, "OUT" if out else "IN", kind,
                     halted, is_midi, st))

print(f"{'intf':>4} {'ep':>6} {'dir':>4} {'type':>5} {'halted':>7}  midi  raw")
for n, addr, d, k, h, m, st in rows:
    print(f"{n:>4} 0x{addr:02x}   {d:>4} {k:>5} {str(h):>7}  {str(m):>5}  {st}")

if not do_clear:
    print("\n(re-run with --clear to clear FEATURE_ENDPOINT_HALT on MIDI eps)")
    sys.exit(0)

APP = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester"


def idn():
    r = subprocess.run([f"{APP}/.venv/bin/python", "csd_diag.py", "--backend",
                        "rawmidi", "--timeout", "6", "*IDN?"],
                       capture_output=True, text=True, cwd=APP, timeout=20)
    o = (r.stdout or "").strip()
    return "ALIVE" if "ClearSound" in o else f"DEAD ({o[:40]})"


for n, addr, d, k, h, m, st in rows:
    if not m or not h:
        continue
    try:
        dev.ctrl_transfer(BM_OUT_STD_EP, CLEAR_FEATURE, EP_FEATURE_HALT, addr,
                          b"", timeout=3_000)
        print(f"CLEAR_FEATURE(ENDPOINT_HALT) on 0x{addr:02x} (intf {n})")
    except usb.core.USBError as ex:
        print(f"clear on 0x{addr:02x} failed: {ex}")

time.sleep(1)
print(f"SCPI after clear: {idn()}")
