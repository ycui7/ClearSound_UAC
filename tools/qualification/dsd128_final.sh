#!/bin/bash
# DSD128 DoP @352.8k with open-retry; plus DSD64 confirmation in same session.
set -u
S=/home/ycui/.hermes/cache/scratch
PY=/home/ycui/.hermes/hermes-agent/venv/bin/python

pkill -x xrun 2>/dev/null; sleep 1

$PY - <<'EOF'
import struct
S = "/home/ycui/.hermes/cache/scratch"
rate, secs = 352800, 6
nfr = rate * secs
def bit(n): return 1 if (n // 1800) % 2 == 0 else 0   # DSD128: half period 1800 bits @5.6448MHz
with open(S + "/dop128.raw", "wb") as f:
    buf = bytearray()
    for fr in range(nfr):
        m = (0x05, 0xFA)[fr & 1]
        d0 = d1 = 0
        for k in range(8):
            if bit(fr * 16 + k):     d0 |= 1 << (7 - k)
            if bit(fr * 16 + 8 + k): d1 |= 1 << (7 - k)
        word = (m | (d0 << 8)) | ((m | (d1 << 8)) << 16)
        buf += struct.pack("<II", word, word)
        if len(buf) >= (1 << 20):
            f.write(buf); buf = bytearray()
    f.write(buf)
import os
print("dop128.raw bytes:", os.path.getsize(S + "/dop128.raw"), "expect:", nfr * 8)
EOF

( cd ~/XMOS/XTC/15.3.0 && set +u && source ./SetEnv >/dev/null 2>&1
  cd ~/sw-audio-sc/sw_usb_audio/clearsound_uac && exec xrun bin/2AMi2o2xxxxxd/ClearSoundDac_2AMi2o2xxxxxd.xe ) >/dev/null 2>&1 &
LOAD=$!
trap 'kill $LOAD 2>/dev/null' EXIT
for i in $(seq 1 60); do
  DAC_CARD=$(awk '/XMOS/ && /USB-Audio/{gsub(/[^0-9]/,"",$1); print $1; exit}' /proc/asound/cards)
  MIC_CARD=$(awk '/E36dB/ && /USB-Audio/{gsub(/[^0-9]/,"",$1); print $1; exit}' /proc/asound/cards)
  [ -n "$DAC_CARD" ] && [ -n "$MIC_CARD" ] && break
  sleep 1
done
echo "DAC=$DAC_CARD MIC=$MIC_CARD"
sleep 3
sg audio -c "amixer -c $DAC_CARD cset numid=3 -- on; amixer -c $DAC_CARD cset numid=4 -- on; amixer -c $DAC_CARD cset numid=5 -- 192,192; amixer -c $DAC_CARD cset numid=6 -- 192" >/dev/null 2>&1

try() { # $1 tag $2 raw $3 rate
  for a in 1 2 3; do
    ( arecord -q -D hw:$MIC_CARD,0 -f S24_3LE -r 48000 -c 2 -d 4 -t wav $S/d128_$1_$a.wav >/dev/null 2>&1 & )
    sleep 0.4
    ERR=$(sg audio -c "ffmpeg -v error -f s32le -ar $3 -ac 2 -i $2 -f alsa -ac 2 plughw:$DAC_CARD,0" 2>&1)
    sleep 3.4
    [ -z "$ERR" ] && break
  done
  L=$($PY $S/ears_fft.py $S/d128_$1_$a.wav 1568 2>&1 | grep -oE "1568Hz: [-+0-9.]+dBFS +SNR=[+0-9.]+dB" | head -1)
  echo "$1 ($3 Hz): open=${ERR:+err}${ERR:-ok} tone=${L:-ABSENT}"
}
try dsd64  $S/dop_good.raw 176400
try dsd128 $S/dop128.raw  352800
