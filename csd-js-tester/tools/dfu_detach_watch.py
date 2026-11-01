#!/usr/bin/env python3
"""Detach watcher: send DFU DETACH, then record EVERY 20b1 device transition
for 20 s at 200 ms resolution, with dmesg reasons. Answers the only question
that matters right now: does d018 appear at all, and for how long?"""
import subprocess, time, sys, os

def snap():
    out = subprocess.run(["lsusb", "-d", "20b1:"], capture_output=True, text=True).stdout
    ids = []
    for ln in out.splitlines():
        if "ID " in ln:
            ids.append(ln.split("ID ")[1].split()[0])
    return tuple(sorted(ids))

def main():
    import usb.core
    rt = usb.core.find(idVendor=0x20b1, idProduct=0x0018)
    if rt is None:
        sys.exit("no runtime device to detach")
    intf = None
    for cfg_ in rt:
        for i in cfg_:
            if i.bDescriptorType == 4 and i.bInterfaceClass == 0xFE and i.bInterfaceSubClass == 1:
                intf = i.bInterfaceNumber
    if intf is None:
        sys.exit("no DFU interface on runtime device")
    before = snap()
    print(f"before: {before}", flush=True)
    t0 = time.time()
    try:
        rt.ctrl_transfer(0x21, 0x00, 250, intf, b"", timeout=10_000)
        print(f"DETACH sent on intf {intf} at t+{time.time()-t0:.2f}s", flush=True)
    except usb.core.USBError as e:
        print(f"DETACH failed: {e}", flush=True)
    last = before
    seen_d018 = 0.0
    d018_first = None
    while time.time() - t0 < 20:
        now = snap()
        if now != last:
            print(f"t+{time.time()-t0:5.2f}s  {last} -> {now}", flush=True)
            last = now
        if "20b1:d018" in now:
            if d018_first is None:
                d018_first = time.time() - t0
            seen_d018 += 0.2
        time.sleep(0.2)
    print(f"FINAL: {last}")
    if d018_first is not None:
        print(f"D018-FIRST-SEEN at t+{d018_first:.2f}s, visible ~{seen_d018:.1f}s of the window")
    else:
        print("D018-NEVER-APPEARED in 20 s")
    subprocess.run(["dmesg", "--time-format=iso", "-T"], capture_output=True, text=True)
    dm = subprocess.run("dmesg | tail -12", shell=True, capture_output=True, text=True).stdout
    print("----- dmesg tail -----")
    print("\n".join(l for l in dm.splitlines() if "usb" in l.lower() or "20b1" in l) or dm)

main()
