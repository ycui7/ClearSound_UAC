#!/usr/bin/env python3
"""Verbose DFU probe: drive the first few DNLOADs and print every
(status, pollTimeout, state) the device reports, so the failure mode is
OBSERVED rather than theorized. Run from the rig venv (pyusb)."""
import sys, time
import usb.core

BM_OUT, BM_IN = 0x21, 0xA1
DN, GS, CS, AB = 0x01, 0x03, 0x04, 0x06
NAMES = {0:"appIDLE",1:"appDETACH",2:"dfuIDLE",3:"DNLOAD-SYNC",4:"DNBUSY",
         5:"DNLOAD-IDLE",6:"MANIFEST-SYNC",7:"MANIFEST",
         8:"MANIFEST-WAIT-RESET",9:"UPLOAD-IDLE",10:"ERROR",11:"DETACHED"}

dev = usb.core.find(idVendor=0x20b1, idProduct=0xD018)
if dev is None:
    sys.exit("not in DFU mode (bring it up with tools/dfu_flash.py detach path)")
if dev.is_kernel_driver_active(0):
    dev.detach_kernel_driver(0)
dev.set_configuration()
INTF = 0
dev.set_interface_altsetting(interface=INTF, alternate_setting=0)

def gs(tag):
    d = bytes(dev.ctrl_transfer(BM_IN, GS, 0, INTF, 6, timeout=10_000))
    st, poll, state = d[0], d[1] | (d[2] << 8) | (d[3] << 16), d[4]
    print(f"  {tag:28s} raw={d.hex()} status={st} poll={poll}ms "
          f"state={state}/{NAMES.get(state,'?')}", flush=True)
    return st, poll, state

dev.ctrl_transfer(BM_OUT, CS, 0, INTF, b"", timeout=10_000); print("CLRSTATUS")
gs("after CLRSTATUS")
dev.ctrl_transfer(BM_OUT, AB, 0, INTF, b"", timeout=10_000); print("ABORT")
for i in range(5):
    st, poll, state = gs(f"poll{i} after ABORT")
    if poll: time.sleep(poll/1000)
    if state in (2, 5): break

img = open(sys.argv[1], "rb").read()
TS = 64
for blk in range(4):
    chunk = img[blk*TS:(blk+1)*TS].ljust(TS, b"\xff")
    try:
        dev.ctrl_transfer(BM_OUT, DN, blk, INTF, chunk, timeout=60_000)
        print(f"DNLOAD block {blk} accepted", flush=True)
    except usb.core.USBError as e:
        print(f"DNLOAD block {blk} -> USBError {e}", flush=True)
        break
    for i in range(8):
        st, poll, state = gs(f"  blk{blk} poll{i}")
        if poll: time.sleep(poll/1000)
        if state in (2, 5, 10): break
    if state == 10:
        break
