#!/bin/bash
# 44.1k re-test with dynamic card discovery (indices drift across loads).
set -u
S=/home/ycui/.hermes/cache/scratch
PY=/home/ycui/.hermes/hermes-agent/venv/bin/python

( cd ~/XMOS/XTC/15.3.0 && set +u && source ./SetEnv >/dev/null 2>&1
  cd ~/sw-audio-sc/sw_usb_audio/clearsound_uac && exec xrun bin/2AMi2o2xxxxxd/ClearSoundDac_2AMi2o2xxxxxd.xe ) >/dev/null 2>&1 &
LOAD=$!
trap 'kill $LOAD 2>/dev/null' EXIT
for i in $(seq 1 40); do grep -q XMOS /proc/asound/cards && break; sleep 1; done
DAC_CARD=$(awk '/XMOS/ && /USB-Audio/{gsub(/[^0-9]/,"",$1); print $1; exit}' /proc/asound/cards)
MIC_CARD=$(awk '/E36dB/ && /USB-Audio/{gsub(/[^0-9]/,"",$1); print $1; exit}' /proc/asound/cards)
echo "DAC=$DAC_CARD MIC=$MIC_CARD"
grep -q XMOS /proc/asound/cards || { echo CARD-FAIL; exit 1; }
sleep 2
sg audio -c "amixer -c $DAC_CARD cset numid=3 -- on; amixer -c $DAC_CARD cset numid=4 -- on; amixer -c $DAC_CARD cset numid=5 -- 192,192; amixer -c $DAC_CARD cset numid=6 -- 192" >/dev/null 2>&1

for R in 44100 88200 176400 352800; do
  for a in 1 2 3; do
    ( arecord -q -D hw:$MIC_CARD,0 -f S24_3LE -r 48000 -c 2 -d 3 -t wav $S/r441_${R}_$a.wav >/dev/null 2>&1 & )
    sleep 0.3
    ERR=$(sg audio -c "ffmpeg -v error -f lavfi -i sine=frequency=1000:sample_rate=$R:duration=4,volume=0.251 -f alsa -ac 2 plughw:$DAC_CARD,0" 2>&1)
    sleep 3
    L=$($PY $S/ears_fft.py $S/r441_${R}_$a.wav 1000 2>&1 | grep -oE "1000Hz: [-+0-9.]+dBFS" | head -2 | tr '\n' ' ')
    echo "$R try$a: $L ${ERR:0:80}"
    case "$L" in *[0-9]*) [ "${L#*-5}" = "$L" ] && break;; esac
  done
done
