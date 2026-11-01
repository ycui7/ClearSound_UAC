#!/usr/bin/env python3
"""Read DFU state on the REAL interface number (the old probe hardcoded 0 and
got a Pipe error). Read-only: GETSTATUS/GETSTATE do not advance the machine."""
import sys, time
import usb.core

BM_IN = 0xA1
GS, GST = 0x03, 0x05
NAMES = {0:"appIDLE",1:"appDETACH",2:"dfuIDLE",3:"DNLOAD-SYNC",4:"DNBUSY",
         5:"DNLOAD-IDLE",6:"MANIFEST-SYNC",7:"MANIFEST",
         8:"MANIFEST-WAIT-RESET",9:"UPLOAD-IDLE",10:"ERROR",11:"DETACHED"}

vid = int(sys.argv[1], 16) if len(sys.argv) > 1 else 0x20b1
pid = int(sys.argv[2], 16) if len(sys.argv) > 2 else 0xD018

dev = usb.core.find(idVendor=vid, idProduct=pid)
if dev is None:
    sys.exit(f"{vid:04x}:{pid:04x} not present")
try:
    if dev.is_kernel_driver_active(0):
        dev.detach_kernel_driver(0)
except usb.core.USBError:
    pass
try:
    dev.set_configuration()
except usb.core.USBError as e:
    print(f"set_configuration: {e} (continuing)")
cfg = dev.get_active_configuration()
ints = [i for i in cfg if i.bInterfaceClass == 0xFE and i.bInterfaceSubClass == 1]
print(f"config {cfg.bConfigurationValue}, DFU-class interfaces: {[i.bInterfaceNumber for i in ints]}")
if not ints:
    for i in cfg:
        print(f"  intf {i.bInterfaceNumber} class={i.bInterfaceClass:#04x} "
              f"sub={i.bInterfaceSubClass:#04x} proto={i.bInterfaceProtocol:#04x} "
              f"alt={i.bAlternateSetting} neps={len(list(i))}")
    sys.exit("no DFU interface found")
intf = ints[0].bInterfaceNumber

for tag, req in (("GETSTATUS", GS), ("GETSTATE", GST)):
    try:
        d = bytes(dev.ctrl_transfer(BM_IN, req, 0, intf, 6 if req == GS else 1,
                                    timeout=10_000))
        if req == GS:
            print(f"{tag}: raw={d.hex()} status={d[0]} poll={d[1]|(d[2]<<8)|(d[3]<<16)}ms "
                  f"state={d[4]}/{NAMES.get(d[4],'?')} deferred={d[5] if len(d)>5 else '-'}")
        else:
            print(f"{tag}: state={d[0]}/{NAMES.get(d[0],'?')}")
    except usb.core.USBError as e:
        print(f"{tag}: USBError {e}")

# raw config bytes -> DFU functional descriptor (type 0x21) wTransferSize
try:
    raw = bytes(dev.ctrl_transfer(0x80, 0x06, (2 << 8) | cfg.bConfigurationValue,
                                  0, 4096, timeout=5_000))
    i = 0; cur = None
    while i + 1 < len(raw):
        ln, typ = raw[i], raw[i + 1]
        if ln < 2: break
        if typ == 0x04 and ln >= 9: cur = raw[i + 2]
        elif typ == 0x21:
            print(f"DFU functional desc on intf {cur}: {raw[i:i+ln].hex()} "
                  f"(wTransferSize at +5,+6 = {raw[i+5] | (raw[i+6] << 8)})")
        i += ln
except usb.core.USBError as e:
    print(f"config read: {e}")
