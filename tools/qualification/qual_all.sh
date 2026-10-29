#!/bin/bash
# One-shot qualification on current firmware:
#  A) PCM playback at all 8 advertised rates (1 kHz tone, FFT level per rate)
#  B) DoP: DSD64 1568 Hz square via DoP16 in 32-bit frames at 176.4 kHz
#  C) USB volume sweep 0 dB -> -48 dB in 3 dB steps, 1 kHz level tracked
set -u
S=/home/ycui/.hermes/cache/scratch
PY=/home/ycui/.hermes/hermes-agent/venv/bin/python
DAC=plughw:4,0
MIC=hw:3,0
OUT=$S/qual_results.txt
: >$OUT

( cd ~/XMOS/XTC/15.3.0 && set +u && source ./SetEnv >/dev/null 2>&1
  cd ~/sw-audio-sc/sw_usb_audio/clearsound_uac && exec xrun bin/2AMi2o2xxxxxd/ClearSoundDac_2AMi2o2xxxxxd.xe ) >/dev/null 2>&1 &
LOAD=$!
trap 'kill $LOAD 2>/dev/null' EXIT
for i in $(seq 1 40); do grep -q XMOS /proc/asound/cards && break; sleep 1; done
grep -q XMOS /proc/asound/cards || { echo CARD-FAIL | tee -a $OUT; exit 1; }
sleep 2
sg audio -c "amixer -c 4 cset numid=3 -- on; amixer -c 4 cset numid=4 -- on; amixer -c 4 cset numid=5 -- 192,192; amixer -c 4 cset numid=6 -- 192" >/dev/null 2>&1

# ---------- A) PCM rates ----------
for R in 44100 48000 88200 96000 176400 192000 352800 384000; do
  L=""
  for a in 1 2 3; do
    ( arecord -q -D $MIC -f S24_3LE -r 48000 -c 2 -d 2 -t wav $S/qual_r$R.wav >/dev/null 2>&1 & )
    sleep 0.3
    sg audio -c "ffmpeg -v error -f lavfi -i sine=frequency=1000:sample_rate=$R:duration=4,volume=0.251 -f alsa -ac 2 $DAC" >/dev/null 2>&1
    sleep 2
    L=$($PY $S/ears_fft.py $S/qual_r$R.wav 1000 2>&1 | grep -oE "1000Hz: [-+0-9.]+dBFS" | tr '\n' ' ')
    # a "pass" needs a level above -20 dBFS, not merely a parsed number
    TOP=$(echo "$L" | grep -oE "[-+][0-9.]+dBFS" | tr -d 'dBFS+' | sort -g | tail -1)
    awk -v x="${TOP:--99}" 'BEGIN{exit !(x > -20)}' && break
    echo "  rate $R attempt $a level ${TOP:--99} dBFS -- retrying"
  done
  echo "PCM $R: $L" | tee -a $OUT
done

# ---------- B) DoP DSD64 (DoP16-in-32: [7:0]=marker [31:8]=16 DSD bits) ----------
$PY - <<'EOF'
import struct
S = "/home/ycui/.hermes/cache/scratch"
rate, secs = 176400, 4
nfr = rate * secs
mk = (0x05, 0xFA)
# 1568 Hz square on the DSD bitstream: half period = 2.8224e6/2/1568 = 900 bits
half = lambda n: (n // 900) % 2
buf = bytearray()
with open(S + "/dop_dsd64.raw", "wb") as f:
    for fr in range(nfr):
        m = mk[fr & 1]
        d0 = 0
        d1 = 0
        for k in range(8):
            if half(fr * 16 + k):
                d0 |= 1 << (7 - k)
            if half(fr * 16 + 8 + k):
                d1 |= 1 << (7 - k)
        word = m | (d0 << 8) | (d1 << 16)
        buf += struct.pack("<II", word, word)
        if len(buf) >= (1 << 20):
            f.write(buf)
            buf = bytearray()
    f.write(buf)
import os
print("dop raw:", os.path.getsize(S + "/dop_dsd64.raw"), "bytes (expect", nfr * 8, ")")
EOF
( arecord -q -D $MIC -f S24_3LE -r 48000 -c 2 -d 2 -t wav $S/qual_dop.wav >/dev/null 2>&1 & )
sleep 0.3
sg audio -c "ffmpeg -v error -f s32_le -ar 176400 -ac 2 -i $S/dop_dsd64.raw -f alsa -ac 2 $DAC" >/dev/null 2>&1
sleep 2
$PY $S/ears_fft.py $S/qual_dop.wav 1568 2>&1 | tail -6
DOP=$($PY $S/ears_fft.py $S/qual_dop.wav 1568 2>&1 | grep -oE "1568Hz: [-+0-9.]+dBFS" | head -1)
echo "DoP-DSD64@176.4k (1568Hz square): $DOP" | tee -a $OUT

# ---------- C) Volume sweep 0 .. -48 dB, 3 dB steps ----------
sg audio -c "amixer -c 4 cset numid=3 -- on; amixer -c 4 cset numid=4 -- on" >/dev/null 2>&1
for i in $(seq 0 16); do
  V=$((192 - i * 12))
  sg audio -c "amixer -c 4 cset numid=5 -- $V,$V; amixer -c 4 cset numid=6 -- $V" >/dev/null 2>&1
  sleep 0.5
  ( arecord -q -D $MIC -f S24_3LE -r 48000 -c 2 -d 2 -t wav $S/qual_v$i.wav >/dev/null 2>&1 & )
  sleep 0.3
  sg audio -c "ffmpeg -v error -f lavfi -i sine=frequency=1000:sample_rate=48000:duration=3,volume=0.251 -f alsa -ac 2 $DAC" >/dev/null 2>&1
  sleep 2
  L=$($PY $S/ears_fft.py $S/qual_v$i.wav 1000 2>&1 | grep -oE "1000Hz: [-+0-9.]+dBFS" | tr '\n' ' ')
  echo "VOL idx=$i usb=${i}x-3dB usbval=$V mic: $L" | tee -a $OUT
done
echo DONE | tee -a $OUT
