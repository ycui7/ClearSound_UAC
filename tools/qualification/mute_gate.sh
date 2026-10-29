#!/bin/bash
# Mute gate: continuous tone on the CSD, record the EARS mic with mute off and
# on, compare the 1 kHz line. Pass = tone present unmuted, gone when muted.
set -u
S=/home/ycui/.hermes/cache/scratch
DAC=plughw:4,0
MIC=hw:3,0
PY=/home/ycui/.hermes/hermes-agent/venv/bin/python

sg audio -c "amixer -c 4 cset numid=5 -- 192,192; amixer -c 4 cset numid=6 -- 192; amixer -c 4 cset numid=3 -- on; amixer -c 4 cset numid=4 -- on" >/dev/null 2>&1

start_tone() {
    for a in 1 2 3 4 5; do
        sg audio -c "ffmpeg -v error -f lavfi -i 'sine=frequency=1000:sample_rate=48000:duration=30' -f alsa -ac 2 $DAC" >$S/tone_err.log 2>&1 &
        TONE=$!
        sleep 2
        kill -0 $TONE 2>/dev/null && { echo "tone: running (try $a)"; return 0; }
        wait $TONE 2>/dev/null; echo "tone attempt $a exited: $(tr -d '\n' <$S/tone_err.log | cut -c1-90)"
        sleep 2
    done
    echo "tone: DEAD"; return 1
}
start_tone || { kill $TONE 2>/dev/null; exit 1; }
trap 'kill $TONE 2>/dev/null' EXIT

sg audio -c "amixer -c 4 cset numid=3 -- on;  amixer -c 4 cset numid=4 -- on"  >/dev/null 2>&1; sleep 1
sg audio -c "arecord -D $MIC -f S24_3LE -r 48000 -c 2 -d 3 -t wav $S/m_on.wav"  >/dev/null 2>&1

sg audio -c "amixer -c 4 cset numid=3 -- off; amixer -c 4 cset numid=4 -- off" >/dev/null 2>&1; sleep 1
sg audio -c "arecord -D $MIC -f S24_3LE -r 48000 -c 2 -d 3 -t wav $S/m_off.wav" >/dev/null 2>&1

# back to unmuted, then a third capture: proves unmute restores the level
sg audio -c "amixer -c 4 cset numid=3 -- on;  amixer -c 4 cset numid=4 -- on"  >/dev/null 2>&1; sleep 1
sg audio -c "arecord -D $MIC -f S24_3LE -r 48000 -c 2 -d 3 -t wav $S/m_back.wav" >/dev/null 2>&1

kill $TONE 2>/dev/null; trap - EXIT

echo "on   (unmuted):"; $PY $S/ears_fft.py $S/m_on.wav   1000 2>&1 | grep 1000Hz
echo "off  (muted)  :"; $PY $S/ears_fft.py $S/m_off.wav  1000 2>&1 | grep 1000Hz
echo "back (unmuted):"; $PY $S/ears_fft.py $S/m_back.wav 1000 2>&1 | grep 1000Hz
