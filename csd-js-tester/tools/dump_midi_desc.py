#!/usr/bin/env python3
"""Print the real USB-MIDI descriptor facts for the board.

Ground truth for the browser transport: which interface owns the MIDI bulk
pair, and what CS_ENDPOINT subtype each bulk endpoint carries. USB-MIDI 1.0
puts a class-specific CS_ENDPOINT (bType 0x25) after each MIDI data endpoint
with subtype 0x01 (MS_GENERAL) -- the verified client's
find_midi_bulk_endpoints() keys on exactly that, so if this tool does not
agree, one of the two is wrong and the browser transport's discovery rule is
built on sand.

Two earlier versions died here and their output was cited as if it had
answered the CS_ENDPOINT question. It had not: pyusb 1.3.1 has no
Device.get_descriptor(), and an AttributeError on the first attempt was
followed by a run whose parsed tree stops before the raw walk. This version
fetches the raw config bytes through csd_diag._config_bytes(), the same
control-transfer path the verified client uses, and never claims more than it
prints.

Run: sudo ./.venv/bin/python tools/dump_midi_desc.py   (raw control needs root)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import csd_diag as C
import usb.core

AUDIO_SUBCLASS_NAMES = {0x01: "AudioControl", 0x02: "AudioStreaming",
                        0x03: "MIDIStreaming"}
EP_XFER = {0x00: "control", 0x01: "isochronous", 0x02: "bulk", 0x03: "interrupt"}

dev = None
try:
    dev = usb.core.find(idVendor=0x20B1, idProduct=0x0018)
    if dev is None:
        print("board not found by pyusb")
except Exception as e:
    print(f"pyusb unavailable ({e}); the raw sysfs walk below still answers the question")

if dev is not None:
    try:
        cfg = dev.get_active_configuration()
    except Exception as e:
        cfg = None
        print(f"parsed tree unavailable ({e})")
    if cfg is not None:
        print("active configuration:", cfg.bConfigurationValue)
        print("(parsed by pyusb -- class-specific descriptors are dropped by its parser;"
              " the raw walk below is authoritative)")

        for itf in cfg:
            print(f"\ninterface {itf.bInterfaceNumber} alt {itf.bAlternateSetting}: "
                  f"class={itf.bInterfaceClass:02x} subclass={itf.bInterfaceSubClass:02x} "
                  f"({AUDIO_SUBCLASS_NAMES.get(itf.bInterfaceSubClass, '?')}) "
                  f"protocol={itf.bInterfaceProtocol:02x}")
            for ep in itf:
                xfer = EP_XFER.get(ep.bmAttributes & 0x03, "?")
                print(f"   ep 0x{ep.bEndpointAddress:02x} attr=0x{ep.bmAttributes:02x} "
                      f"{xfer} maxpacket={ep.wMaxPacketSize}")

print("\n================ raw config descriptor walk ================")
data = None
try:
    data = C._config_bytes(dev)
    print(f"{len(data)} bytes via GET_DESCRIPTOR (control transfer)")
except Exception as e:
    # A live control transfer on a device the kernel has claims on can fail
    # with EIO. sysfs holds the same bytes the enumeration used -- identical
    # source of truth, no bus traffic. Prefer it over guessing.
    print(f"control transfer failed ({e}); falling back to sysfs descriptors")
    import glob
    cands = glob.glob("/sys/bus/usb/devices/*/idVendor")
    for vp in cands:
        d = os.path.dirname(vp)
        try:
            if open(os.path.join(d, "idVendor")).read().strip() != "20b1":
                continue
            if open(os.path.join(d, "idProduct")).read().strip() != "0018":
                continue
        except OSError:
            continue
        raw = open(os.path.join(d, "descriptors"), "rb").read()
        # 18-byte device descriptor, then the config descriptor
        total = raw[20] | (raw[21] << 8)
        data = raw[18:18 + total]
        print(f"{len(data)} bytes from {d}/descriptors (config wTotalLength={total})")
        break
if data is None:
    print("no raw config descriptor obtained; CS_ENDPOINT question stays open")
    sys.exit(2)

# Walk records, tracking the owning interface, and dump EVERY endpoint plus
# every class-specific endpoint record with the endpoint it follows.
i, n = 0, len(data)
cur_itf = None
cs_records = []           # (offset, subtype, follows_addr, hex)
bulk_eps = []             # (addr, follows_cs_subtype or None, interface)
pending_bulk = None
while i + 2 <= n:
    blen = data[i]
    btype = data[i + 1]
    if blen < 2 or i + blen > n:
        print(f"!! malformed record at {i}: bLength={blen} bType=0x{btype:02x}")
        break
    if btype == 0x04 and blen >= 9:                          # INTERFACE
        # bLength,bType,bInterfaceNumber,bAlternateSetting,bNumEndpoints,
        # bInterfaceClass,bInterfaceSubClass,bInterfaceProtocol,iInterface
        cur_itf = data[i + 2]
        nep, iclass, sub, proto = data[i + 4], data[i + 5], data[i + 6], data[i + 7]
        # Subclass names are only meaningful inside the Audio class (0x01).
        # Any other class uses these numbers for something else, so naming one
        # from the audio table would be a fabricated label.
        subname = AUDIO_SUBCLASS_NAMES.get(sub, "?") if iclass == 0x01 else "-"
        print(f"  IFACE  if#{cur_itf} alt={data[i+3]} nEP={nep} class=0x{iclass:02x} "
              f"subclass=0x{sub:02x} ({subname}) proto=0x{proto:02x}")
        pending_bulk = None
    elif btype == 0x05 and blen >= 7:                        # ENDPOINT
        addr = data[i + 2]
        xfer = EP_XFER.get(data[i + 3] & 0x03, "?")
        mps = data[i + 4] | (data[i + 5] << 8)
        pending_bulk = None
        if xfer == "bulk":
            pending_bulk = [addr, None, cur_itf]
            bulk_eps.append(pending_bulk)
        print(f"    EP     if#{cur_itf} 0x{addr:02x} {xfer} maxpacket={mps}")
    elif btype == 0x25 and blen >= 3:                        # CS_ENDPOINT
        sub = data[i + 2]
        follows = pending_bulk[0] if pending_bulk else None
        if pending_bulk:
            pending_bulk[1] = sub
        # A CS_ENDPOINT after an ISOC endpoint is the audio TYPE_I record; only
        # one after a BULK endpoint marks MIDI data. Print both -- collapsing
        # them is what let the two get confused in the first place.
        tag = f"0x{follows:02x}" if follows is not None else "(non-bulk ep)"
        cs_records.append((i, sub, follows, bytes(data[i:i + blen]).hex()))
        print(f"    CS_EP  follows {tag} subtype={sub}  {bytes(data[i:i + blen]).hex()}")
        pending_bulk = None
    i += blen

print("\nbulk endpoints: ", [(hex(a), f"if#{iff}",
                             f"CS subtype {cs}" if cs is not None else "NO CS_ENDPOINT")
                            for a, cs, iff in bulk_eps])
for off, sub, follows, hx in cs_records:
    tag = f"0x{follows:02x}" if follows is not None else "(non-bulk ep)"
    print(f"CS_ENDPOINT @ {off:4d}: follows {tag} subtype={sub} {hx}")

print("\nverified client's find_midi_bulk_endpoints on these exact bytes:",
      tuple(hex(x) if x is not None else None
            for x in C.find_midi_bulk_endpoints(data)))
print("(the client keys on CS subtype 0x01 after a bulk endpoint; if this "
      "prints the MIDI pair, the browser transport's facts are confirmed)")
