"""Are our FD coefficient words actually inside the flashed .xe?

Takes the first long run of distinct words from the generated header, builds the
little-endian byte pattern, and searches the .xe. A miss means the image does
not carry the tables we think we shipped (stale build, linker/placement, or a
flash-read problem) -- which would explain device garbage with host-correct math.
"""
import re
import sys

HDR = "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/src/extensions/csdfd_48000.h"
XE = sys.argv[1] if len(sys.argv) > 1 else \
    "/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/bin/2AMi2o2xxxxxd/ClearSoundDac_2AMi2o2xxxxxd.xe"

txt = open(HDR).read()
m = re.search(r"coefs_csdfd_48000\s*\[\s*\d*\s*\]\s*=\s*\{(.*?)\}\s*;", txt, re.S)
if not m:
    sys.exit("coefs_csdfd_48000 array not found in header")
words = [int(w, 0) for w in re.findall(r"-?\d+", m.group(1))]
print("table words:", len(words), " nonzero:", sum(1 for w in words if w))

# first run of 24 consecutive nonzero, mutually-distinct words (robust signature)
run, cand = [], None
for w in words:
    if w == 0 or (run and w in run):
        if len(run) >= 12:
            cand = run
            break
        run = []
        continue
    run.append(w)
    if len(run) >= 24:
        cand = run
        break
if not cand:
    sys.exit("no distinctive run found -- table looks degenerate")

blob = open(XE, "rb").read()
pat = b"".join((w & 0xFFFFFFFF).to_bytes(4, "little") for w in cand)
off = blob.find(pat)
print("signature len: %d words" % len(cand))
print("first words:", ["0x%08X" % (w & 0xFFFFFFFF) for w in cand[:6]])
print("image size:", len(blob))
if off >= 0:
    print("FOUND at offset 0x%X  -> coefficients ARE in the image" % off)
else:
    print("NOT FOUND -> the flashed image does NOT contain these coefficients")
