#!/usr/bin/env python3
"""Leave DFU mode and reboot into the runtime image.

Exit path is sourced, not guessed: in dfuIDLE, DFU_DETACH runs flash_deinit()
and sets deferred REBOOT (lib_dfu/src/dfu.xc:304-309), and the same happens on
a host-initiated bus reset in dfuIDLE (:314-321). Without this the board sits
at d018 forever after a successful download."""
import sys, time
import usb.core

BM_OUT = 0x21
DETACH, ABORT, GS = 0x00, 0x06, 0x03
NAMES = {0:"appIDLE",1:"appDETACH",2:"dfuIDLE",3:"DNLOAD-SYNC",4:"DNBUSY",
         5:"DNLOAD-IDLE",6:"MANIFEST-SYNC",7:"MANIFEST",8:"MANIFEST-WAIT-RESET",
         10:"ERROR",11:"DETACHED"}

def present(pid):
    return usb.core.find(idVendor=0x20b1, idProduct=pid) is not None

dev = present(0xD018) and usb.core.find(idVendor=0x20b1, idProduct=0xD018)
if dev is None:
    print("already out of DFU mode" if present(0x0018) else "no device at all")
    sys.exit(0 if present(0x0018) else 1)
try:
    if dev.is_kernel_driver_active(0):
        dev.detach_kernel_driver(0)
except usb.core.USBError:
    pass
try:
    dev.set_configuration()
except usb.core.USBError:
    pass
INTF = 0
d = bytes(dev.ctrl_transfer(0xA1, GS, 0, INTF, 6, timeout=10_000))
print(f"pre-exit state: {d[4]}/{NAMES.get(d[4],'?')} status={d[0]}")
if d[4] not in (2, 5):
    sys.exit(f"refusing: not in a detachable state ({NAMES.get(d[4],'?')})")

for tag, req in (("DETACH", DETACH), ("ABORT", ABORT)):
    try:
        dev.ctrl_transfer(BM_OUT, req, 250, INTF, b"", timeout=10_000)
        print(f"{tag} sent")
        break
    except usb.core.USBError as e:
        print(f"{tag} -> {e}")

t0 = time.time()
while time.time() - t0 < 60:
    if present(0x0018) and not present(0xD018):
        print(f"RUNTIME-PID-BACK after {time.time()-t0:.1f}s")
        sys.exit(0)
    time.sleep(0.5)
print("TIMEOUT: runtime PID did not return within 60 s")
sys.exit(1)
