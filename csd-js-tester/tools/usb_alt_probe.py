#!/usr/bin/env python3
"""Read the CURRENT alt setting of every interface straight from the device
(standard GET_CUR to the interface recipient -- read-only, does not advance
any state machine).

Why: the hypothesis for the post-DFU-exit state is that the deferred REBOOT
re-attaches so quickly the host treats it as a bus reset rather than a
disconnect, keeps its 'already configured' view, and therefore never re-sends
SET_INTERFACE(alt=1). Alt 0 has no bulk endpoints, so EP0 still answers
descriptors (card appears, MIDI node exists) while no audio or MIDI data can
flow. Prediction: after a DFU-exit boot the audio/MIDI interfaces report
alt=0; after a monitor-reset boot they report alt=1."""
import sys
import usb.core

BM_IN_STD_INTF = 0x81        # device->host, standard, interface recipient
GET_CUR = 0x0a

dev = usb.core.find(idVendor=0x20b1, idProduct=0x0018)
if dev is None:
    sys.exit("runtime PID (20b1:0018) not present")
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
print(f"active config: {cfg.bConfigurationValue}")
for i in cfg:
    n = i.bInterfaceNumber
    try:
        cur = dev.ctrl_transfer(BM_IN_STD_INTF, GET_CUR, 0, n, 1, timeout=3_000)[0]
    except usb.core.USBError as e:
        cur = f"err({e})"
    alts = getattr(i, "bAlternateSetting", 0)
    neps = len(list(i))
    kind = {0xFE: "DFU", 0x01: "AUDIO", 0x07: "MIDI"}.get(i.bInterfaceClass,
                                                          f"class{ i.bInterfaceClass:#04x}")
    print(f"  intf {n:2d} {kind:5s} alt={alts} neps={neps:2d}  device-said-current-alt={cur}")
