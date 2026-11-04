#!/usr/bin/env python3
"""What formats/rates does the CSD actually negotiate at hw:3,0?

Why not ffmpeg: it SILENTLY FALLS BACK. Asked for s32 it "succeeded" while
/proc hw_params showed S16_LE. A bit-perfect rig built on that would test a
16-bit wire against a 32-bit golden file and report PASS. So probe libasound
directly.

Why format NAMES resolved by ALSA and not enum constants: a first version
hardcoded S32_LE=14 -- that is U32_LE -- and confidently reported the device
"unsupported S32" for every rate. Never hand-copy the snd_pcm_format_t enum.
"""
import ctypes

alsa = ctypes.CDLL("libasound.so.2")
alsa.snd_pcm_format_value.restype = ctypes.c_int
alsa.snd_pcm_format_value.argtypes = [ctypes.c_char_p]
alsa.snd_pcm_format_name.restype = ctypes.c_char_p
alsa.snd_pcm_format_name.argtypes = [ctypes.c_int]

NAMES = (b"S32_LE", b"S24_LE", b"S24_3LE", b"S16_LE")

pcm = ctypes.c_void_p()
rc = alsa.snd_pcm_open(ctypes.byref(pcm), b"hw:3,0", 0, 0)
if rc < 0:
    print("open hw:3,0 failed:", alsa.snd_strerror(rc))
    raise SystemExit(1)

hp = ctypes.c_void_p()
alsa.snd_pcm_hw_params_malloc(ctypes.byref(hp))

print("  fmt       rate     result")
for nm in NAMES:
    fmt = alsa.snd_pcm_format_value(nm)
    resolved = alsa.snd_pcm_format_name(fmt).decode()
    assert resolved == nm.decode(), f"resolver lied: {nm} -> {resolved}"
    for rate in (384000, 192000, 48000):
        alsa.snd_pcm_hw_params_any(pcm, hp)
        if alsa.snd_pcm_hw_params_test_format(pcm, hp, fmt) != 0:
            verdict = "FORMAT-UNSUPPORTED"
        else:
            alsa.snd_pcm_hw_params_set_format(pcm, hp, fmt)
            if alsa.snd_pcm_hw_params_test_rate(pcm, hp, rate, 0) != 0:
                verdict = "RATE-UNSUPPORTED-WITH-FORMAT"
            else:
                alsa.snd_pcm_hw_params_set_rate(pcm, hp, rate, 0)
                got = ctypes.c_uint(0)
                alsa.snd_pcm_hw_params_get_rate(hp, ctypes.byref(got), 0)
                verdict = f"OK negotiated={got.value}"
        print(f"  {resolved:7s} @{rate:6d}: {verdict}")

alsa.snd_pcm_close(pcm)
