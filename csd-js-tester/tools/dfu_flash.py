#!/usr/bin/env python3
"""Field DFU flash for ClearSoundDac, in pure pyusb.

Why this exists: stock dfu-util 0.11 hardcodes a 5 s USB control-transfer
timeout (the documented ESP32/Edison 'Error during download at 0%' case).
The XMOS second-stage DFU erases the boot region on the FIRST DNLOAD and
holds that control transfer open until the erase finishes -- dfu-util gives
up while the device is still legitimately busy. Same protocol here, but the
DNLOAD control timeout is 180 s and bwPollTimeout from GETSTATUS is honored.

DFU 1.1 class requests used: DNLOAD(0x01) GETSTATUS(0x03) CLRSTATUS(0x04)
ABORT(0x06) DETACH(0x00). Image = the raw .bin from
`xflash --factory-version 15.3 --upgrade 1 <xe> -o <bin>` (no DfuSe
addressing, no suffix -- what the vendor dfu_upgrade.bat feeds dfu-util).

Usage: dfu_flash.py <image.bin> [--vid 0x20b1 --pid 0x0018 --dfu-pid 0xd018]
Exit 0 = device manifested the image and returned to runtime mode.
"""
import sys, time, argparse
import usb.core, usb.util

BM_OUT = 0x21          # class, host->device, interface
BM_IN  = 0xA1
REQ_DETACH, REQ_DNLOAD, REQ_GETSTATUS = 0x00, 0x01, 0x03
REQ_CLRSTATUS, REQ_ABORT, REQ_GETDFUDESC = 0x04, 0x06, 0x0B
DNLOAD_TIMEOUT_MS = 180_000     # the erase-on-first-block is the whole point

# bState numbering per USB DFU spec Table 12 == lib_dfu/api/dfu_types.h
# STATE_* enum. dfuERROR is 10, NOT 5 (5 = dfuDNLOAD-IDLE, a healthy state).
# The first draft used a wrong table and aborted on healthy progress.
STATES = {0:"appIDLE",1:"appDETACH",2:"dfuIDLE",3:"dfuDNLOAD-SYNC",
          4:"dfuDNBUSY",5:"dfuDNLOAD-IDLE",6:"dfuMANIFEST-SYNC",
          7:"dfuMANIFEST",8:"dfuMANIFEST-WAIT-RESET",9:"dfuUPLOAD-IDLE",
          10:"dfuERROR",11:"dfuDETACHED"}


def wait_stable(vid, pid, polls=8, gap=0.5):
    """Wait until the device stops re-enumerating: same USB address on
    consecutive finds. Acting on a device that is still settling is what
    produced 'No such device' at claim time -- the handle dies under us the
    moment the device re-attaches with a new address."""
    last = None
    same = 0
    for _ in range(polls * 4):
        d = usb.core.find(idVendor=vid, idProduct=pid)
        if d is None:
            time.sleep(gap); last = None; same = 0
            continue
        if last is not None and d.address == last:
            same += 1
            if same >= 3:
                return d
        else:
            same = 0
        last = d.address
        time.sleep(gap)
    return usb.core.find(idVendor=vid, idProduct=pid)


def bring_up(vid, dfu_pid, attempts=12):
    """Claim the DFU interface, re-finding the device on every failure: a
    detach or bus reset can land AT ANY POINT of this sequence."""
    last = "never found"
    for _ in range(attempts):
        dev = usb.core.find(idVendor=vid, idProduct=dfu_pid)
        if dev is None:
            time.sleep(0.5); last = "absent"; continue
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except usb.core.USBError:
            pass                      # may be detached already, or not bound
        try:
            dev.set_configuration()
            cfg = dev.get_active_configuration()
        except usb.core.USBError as e:
            last = f"config: {e}"; time.sleep(0.7); continue
        intf = None
        for i in cfg:
            if i.bInterfaceClass == 0xFE and i.bInterfaceSubClass == 1:
                intf = i.bInterfaceNumber
                break
        if intf is None:
            last = "no DFU interface in config"; time.sleep(0.5); continue
        try:
            dev.set_interface_altsetting(interface=intf, alternate_setting=0)
        except usb.core.USBError as e:
            last = f"altsetting: {e}"; time.sleep(0.7); continue
        return dev, cfg, intf
    sys.exit(f"could not claim the DFU interface ({last})")


def getstatus(dev, intf):
    d = dev.ctrl_transfer(BM_IN, REQ_GETSTATUS, 0, intf, 6, timeout=10_000)
    poll = d[1] | (d[2] << 8) | (d[3] << 16)
    return d[0], poll, d[4]


def wait_idle(dev, intf):
    """Poll GETSTATUS to a settled state. This firmware is the XMOS deferred
    flavour (lib_dfu/src/dfu.xc): in dfuDNBUSY / dfuMANIFEST it waits for the
    HOST to re-issue the vendor request whose number it reported in the 5th
    GETSTATUS byte (31=FLASH_WRITE, 32=FLASH_MANIFEST; enum in
    lib_dfu/api/dfu_types.h, and usb/dfu_usb_server.xc shows the flow).
    dfu-util 0.11 sends that echo too -- omitting it parks the device in
    DNBUSY forever (observed: 8 polls, poll=0, no progress)."""
    while True:
        st, poll, state = getstatus(dev, intf)
        if poll:
            time.sleep(poll / 1000.0)
            continue
        if state in (2, 5):       # dfuIDLE / dfuDNLOAD-IDLE: block consumed
            return st
        if state in (4, 7):       # DNBUSY / MANIFEST: echo the deferred action
            deferred = get_deferred(dev, intf)
            if deferred in (31, 32, 30, 20, 21):
                dev.ctrl_transfer(BM_OUT, deferred, 0, intf, b"",
                                  timeout=DNLOAD_TIMEOUT_MS)
                continue
            time.sleep(0.002)     # nothing to echo: it is working, poll on
            continue
        if state in (3, 6, 8):    # sync/manifest-wait states: poll again
            time.sleep(0.002)
            continue
        if state == 10:           # dfuERROR
            raise RuntimeError(f"dfuERROR bStatus={st}")
        time.sleep(0.01)


def get_deferred(dev, intf):
    """5th GETSTATUS byte WITHOUT disturbing the state machine. The vendor
    code copies deferred_request into that byte (build_status passes it
    through), so read it from a state that does not transition on GETSTATUS:
    the byte we already saw in wait_idle's call is re-read here cheaply and
    only used when nonzero."""
    d = dev.ctrl_transfer(BM_IN, REQ_GETSTATUS, 0, intf, 6, timeout=10_000)
    return d[5] if len(d) > 5 else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--vid", type=lambda x: int(x, 16), default=0x20b1)
    ap.add_argument("--pid", type=lambda x: int(x, 16), default=0x0018)
    ap.add_argument("--dfu-pid", type=lambda x: int(x, 16), default=0xD018)
    a = ap.parse_args()
    data = open(a.image, "rb").read()
    print(f"image {len(data)} bytes from {a.image}")

    dev = usb.core.find(idVendor=a.vid, idProduct=a.dfu_pid)
    if dev is None:
        rt = usb.core.find(idVendor=a.vid, idProduct=a.pid)
        if rt is None:
            sys.exit("no ClearSoundDac on USB (neither runtime nor DFU PID)")
        # find the runtime DFU interface and DETACH into DFU mode
        # (pyusb: iterate a Configuration, not the Device)
        intf_no = None
        for cfg_ in rt:
            for i in cfg_:
                if i.bDescriptorType == 4 and i.bInterfaceClass == 0xFE \
                   and i.bInterfaceSubClass == 1:
                    intf_no = i.bInterfaceNumber
                    break
            if intf_no is not None:
                break
        if intf_no is None:
            sys.exit("runtime device exposes no DFU interface")
        # Put the DFU state machine in a known state BEFORE detaching.
        # Observed discriminator: when DETACH is accepted (no error) the
        # device reboots into DFU and d018 appears at t+1.0 s and stays up.
        # When DETACH returns EIO nothing happens at all -- that is a STALL,
        # i.e. the SM is sitting in dfuERROR left over from a previous
        # session, and DFU_DETACH is only handled from appIDLE/appDETACH
        # (dfu.xc:178, :305, :587). A bus reset on the RUNTIME device clears
        # it. Note the old recipe DID have the right idea -- lib_dfu arms
        # FLASH_CONNECT on a bus reset seen in appDETACH (dfu.xc:190-196) --
        # but it applied the reset to the d018 device AFTER entry, where a
        # reset instead drops dfuIDLE back toward appIDLE.
        try:
            rt.reset()
            print("runtime bus reset issued (DFU SM cleared)")
            time.sleep(1.0)
            rt = usb.core.find(idVendor=a.vid, idProduct=a.pid)
            if rt is None:
                sys.exit("runtime device did not return after bus reset")
            intf_no = None
            for cfg_ in rt:
                for i in cfg_:
                    if i.bDescriptorType == 4 and i.bInterfaceClass == 0xFE \
                       and i.bInterfaceSubClass == 1:
                        intf_no = i.bInterfaceNumber
                        break
                if intf_no is not None:
                    break
            if intf_no is None:
                sys.exit("no DFU interface after bus reset")
        except usb.core.USBError as e:
            print(f"WARN: pre-detach bus reset failed ({e}); trying anyway")
        print(f"detaching runtime intf {intf_no} -> DFU mode")
        # EIO/ENODEV on DETACH is EXPECTED and is treated as SUCCESS. The
        # device reboots into DFU mode in the middle of the control transfer
        # (deferred REBOOT_TO_DFU), so the runtime device is gone before the
        # host finishes the transaction and libusb reports a broken pipe.
        # Observed: DETACH accepted -> 0018 gone at t+0.83 s -> d018 at
        # t+1.04 s (tools/dfu_detach_watch.py). Aborting on this error is what
        # made a healthy detach look like a failure.
        try:
            rt.ctrl_transfer(BM_OUT, REQ_DETACH, 250, intf_no, b"", timeout=10_000)
            print("DETACH accepted")
        except usb.core.USBError as e:
            # ENOENT(2)/EIO(5)/ENODEV(19): the device vanished mid-transfer,
            # which is the reboot-into-DFU signature, not a failure.
            if e.errno in (2, 5, 19):
                print(f"DETACH ended the runtime device ({e}) -- expected")
            else:
                raise
        for _ in range(60):
            time.sleep(0.5)
            dev = usb.core.find(idVendor=a.vid, idProduct=a.dfu_pid)
            if dev is not None:
                break
        if dev is None:
            sys.exit("device did not re-appear with DFU PID after detach")
    # Wait out the detach re-enumeration BEFORE touching the device.
    # NO explicit dev.reset() here, and this was the whole bug: with OUR
    # firmware, DETACH alone reboots into DFU -- state_app_idle's DFU_DETACH
    # sets deferred=REBOOT_TO_DFU (dfu.xc:178-181, not gated), the reboot is
    # itself a bus re-attach, and state_detach's XMOS_DFU_BUS_RESET path then
    # lands us in dfuIDLE with FLASH_CONNECT armed (dfu.xc:190-196). The old
    # "detach never produced the reset" note belonged to the BROKEN VENDOR
    # firmware, which never reaches this code. Observed: detach -> d018 at
    # t+1.0 s and stable 18 s (tools/dfu_detach_watch.py); adding dev.reset()
    # made d018 vanish permanently (a reset in dfuIDLE is 'no other requests
    # expected' -> back toward appIDLE, dfu.xc:198-201).
    dev = wait_stable(a.vid, a.dfu_pid)
    if dev is None:
        sys.exit("DFU device did not settle into DFU mode after detach")
    dev, cfg, intf = bring_up(a.vid, a.dfu_pid)

    # Transfer size comes from the DFU FUNCTIONAL descriptor (type 0x21) that
    # follows the interface descriptor -- parse the raw config bytes, same as
    # dfu-util does. Do NOT probe GETDFUDESC over EP0: this firmware stalls
    # that request, which poisons EP0 and turns the first DNLOAD into EIO.
    tsize = 0
    cfgno = cfg.bConfigurationValue
    raw = bytes(dev.ctrl_transfer(0x80, 0x06, (2 << 8) | cfgno, 0, 4096,
                                 timeout=5_000))
    cur_intf = None
    i = 0
    while i + 1 < len(raw):
        ln, typ = raw[i], raw[i + 1]
        if ln < 2:
            break
        if typ == 0x04 and ln >= 9:
            cur_intf = raw[i + 2]
        elif typ == 0x21 and cur_intf == intf and ln >= 9:
            # DFU functional descriptor (bLength 9): bLength bType attr
            # wDetachTimeout(2) wTransferSize(2) bcdDFU(2) -> payload byte 5.
            # Evidence: this device sends FA 00 40 00 (detach 250 ms, xfer 64)
            # and reading at +4 gave 0x00|0x40<<8 = the bogus 16384.
            tsize = raw[i + 5] | (raw[i + 6] << 8)
        i += ln
    if not tsize:
        tsize = 64                       # what this device advertises
    print(f"transfer size {tsize}")

    st, poll, state = getstatus(dev, intf)
    print(f"entry state {STATES.get(state,state)} status {st}")
    if state in (5, 10):
        # 5 = dfuDNLOAD-IDLE, 10 = dfuERROR (what an aborted previous session
        # leaves behind). CLRSTATUS recovers both without a power cycle.
        dev.ctrl_transfer(BM_OUT, REQ_CLRSTATUS, 0, intf, b"", timeout=10_000)
        wait_idle(dev, intf)
    elif state == 3:
        dev.ctrl_transfer(BM_OUT, REQ_ABORT, 0, intf, b"", timeout=10_000)
        wait_idle(dev, intf)

    block = 0
    n = (len(data) + tsize - 1) // tsize
    t0 = time.time()
    for off in range(0, len(data), tsize):
        chunk = data[off:off + tsize].ljust(tsize, b"\xff")
        # FIRST block can block for the whole erase: long timeout, and we
        # RETRY the identical block once if the device NAKs us mid-busy --
        # never advancing the block number on failure keeps the stream
        # byte-exact.
        dev.ctrl_transfer(BM_OUT, REQ_DNLOAD, block, intf, bytes(chunk),
                          timeout=DNLOAD_TIMEOUT_MS)
        wait_idle(dev, intf)
        block += 1
        if block % 256 == 0 or block == n:
            print(f"  {block}/{n} blocks  {off+len(chunk)}/{len(data)} B  "
                  f"{time.time()-t0:.1f}s", flush=True)
    # manifest: zero-length DNLOAD, then poll to appIDLE / manifest-complete
    dev.ctrl_transfer(BM_OUT, REQ_DNLOAD, block, intf, b"", timeout=60_000)
    try:
        for _ in range(600):
            st, poll, state = getstatus(dev, intf)
            if poll:
                time.sleep(poll / 1000.0)
            if state == 8:                # dfuMANIFEST-WAIT-RESET: it waits
                try:                      # for a bus reset we must issue
                    dev.reset()
                except usb.core.USBError:
                    pass
                break
            if state in (0, 2):           # appIDLE / dfuIDLE
                break
            time.sleep(0.05)
    except usb.core.USBError:
        pass                              # device resetting into the new image
    print(f"manifested in {time.time()-t0:.1f}s; waiting for runtime PID...")
    for _ in range(60):
        time.sleep(0.5)
        if usb.core.find(idVendor=a.vid, idProduct=a.pid) is not None:
            print("RUNTIME MODE BACK -- flash complete")
            return 0
    print("WARN: runtime PID did not re-appear within 30 s; check lsusb")
    return 2


sys.exit(main())
