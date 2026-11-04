#!/usr/bin/env python3
"""Is the loopback card itself lossy, independent of the rig?

Pure ALSA, two threads, NO comparator, NO numpy, NO schedule: write an
absolutely monotonic counter into every frame, read it back, and count how many
expected values never appear. If aloop loses 48-frame runs on its own, the card
(or the host ALSA path) is the fault and no rig or firmware conclusion stands.
The counter is the payload, so a lost run is visible with zero analysis logic.
"""
import ctypes, sys, time
sys.path.insert(0, "rig")
import bit_perfect_noise as B

alsa = B.alsa
RATE, CH, BPB = 384000, 2, 8
CARD = sys.argv[1] if len(sys.argv) > 1 else "4"
MIN = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
CHUNK = 32768


def open_pcm(direction, dev):
    """Use the RIG's own opener. Deliberately not a hand-rolled one: the whole
    point is to reproduce the rig's host path, and a second implementation that
    negotiates differently proves nothing about the rig."""
    import os
    os.environ["CSD_PLAY_DEV"] = str(B.PLAY_DEV)
    os.environ["CSD_CAP_DEV"] = str(B.CAP_DEV)
    return B.open_pcm(direction, "playback" if not direction else "capture")


import numpy as np
play, pper, pbuf = open_pcm(0, B.PLAY_DEV)
cap, cper, cbuf = open_pcm(1, B.CAP_DEV)
stop = __import__("threading").Event()


def writer():
    alsa.snd_pcm_start(play)
    n = 0
    while not stop.is_set():
        idx = np.arange(n, n + CHUNK, dtype=np.uint64)
        payload = (idx & np.uint64(0xFFFFFF)).astype(np.uint32)
        buf = np.zeros((CHUNK, CH), dtype=np.uint32)
        buf[:, 0] = payload
        buf[:, 1] = payload
        off = 0
        data = memoryview(buf.astype(np.uint32).tobytes())
        while off < len(data) and not stop.is_set():
            av = alsa.snd_pcm_avail_update(play)
            if av < 0:
                alsa.snd_pcm_recover(play, av, 1)
                alsa.snd_pcm_start(play)
                continue
            room = min(len(data) - off, av * BPB)
            if room <= 0:
                time.sleep(0.0005)
                continue
            w = alsa.snd_pcm_writei(play, data[off:off + room], room // BPB)
            if w < 0:
                alsa.snd_pcm_recover(play, w, 1)
                alsa.snd_pcm_start(play)
                continue
            off += w * BPB
        n += CHUNK


th = __import__("threading").Thread(target=writer, daemon=True)
th.start()
alsa.snd_pcm_start(cap)
buf = (ctypes.c_char * (CHUNK * BPB))()
seen = np.zeros(0, dtype=np.int64)
allseen = []
t0 = time.monotonic()
reads = 0
while time.monotonic() - t0 < MIN * 60:
    n = alsa.snd_pcm_readi(cap, buf, CHUNK)
    if n <= 0:
        alsa.snd_pcm_recover(cap, n if n < 0 else -1, 1)
        alsa.snd_pcm_start(cap)
        continue
    reads += 1
    a = np.frombuffer(bytes(buf[:n * BPB]), dtype=np.uint32).reshape(n, CH)
    allseen.append(a[:, 0].astype(np.int64))
stop.set()
th.join(timeout=5)
v = np.concatenate(allseen)
nz = v[v != 0]
print(f"\nreads={reads} captured={len(v):,} nonzero={len(nz):,} "
      f"zeros={len(v)-len(nz):,}")
if len(nz):
    d = np.diff(nz)
    jumps = int((d != 1).sum())
    lost = int((d[d != 1] - 1).sum())
    print(f"distinct jumps (non-consecutive)={jumps} frames_lost_estimate={lost}")
    print(f"span: first={nz[0]} last={nz[-1]} expected_span={nz[-1]-nz[0]+1:,}")
    print(f"missing vs own span = {int(nz[-1]-nz[0]+1) - len(np.unique(nz)):,}")
    if jumps:
        big = nz[:-1][d != 1][:8]
        print("first jump positions:", [int(x) for x in big])
