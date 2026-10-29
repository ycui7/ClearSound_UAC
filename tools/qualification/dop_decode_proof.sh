#!/bin/bash
# Decisive DoP decode proof:
#   payload bytes alternate 0x55 / 0xAA
#   -> DSD stream: exactly 50% duty => DoP decode = SILENCE (mic floor ~-54)
#   -> as PCM16 words: samples alternate ~+21.7k / -22.0k => ~-4 dBFS 88k square
#      => huge aliased splatter at the mic (>>floor) if codec did NOT decode DoP
set -u
S=/home/ycui/.hermes/cache/scratch
PY=/home/ycui/.hermes/hermes-agent/venv/bin/python

pkill -x xrun 2>/dev/null; sleep 1

$PY - <<'EOF'
import struct
S = "/home/ycui/.hermes/cache/scratch"
fs = 176400; secs = 4
nfr = fs * secs
with open(S + "/dp_decode.raw", "wb") as f:
    buf = bytearray()
    for fr in range(nfr):
        m = (0x05, 0xFA)[fr & 1]
        d0 = 0x55 if (fr & 1) == 0 else 0xAA
        d1 = 0xAA if (fr & 1) == 0 else 0x55
        word = (m | (d0 << 8)) | ((m | (d1 << 8)) << 16)
        buf += struct.pack("<II", word, word)
        if len(buf) >= (1 << 20): f.write(buf); buf = bytearray()
    f.write(buf)
print("dp_decode.raw", (22))
EOF

( cd ~/XMOS/XTC/15.3.0 && set +u && source ./SetEnv >/dev/null 2>&1
  cd ~/sw-audio-sc/sw_usb_audio/clearsound_uac && exec xrun bin/2AMi2o2xxxxxd/ClearSoundDac_2AMi2o2xxxxxd.xe ) >/dev/null 2>&1 &
LOAD=$!
trap 'kill $LOAD 2>/dev/null' EXIT
for i in $(seq 1 60); do
  DAC_CARD=$(aplay -l 2>/dev/null | sed -n 's/^card \([0-9]*\):.*XMOS.*/\1/p' | head -1)
  MIC_CARD=$(arecord -l 2>/dev/null | sed -n 's/^card \([0-9]*\):.*E36dB.*/\1/p' | head -1)
  [ -n "$DAC_CARD" ] && [ -n "$MIC_CARD" ] && break
  sleep 1
done
echo "DAC=$DAC_CARD MIC=$MIC_CARD"
sleep 3
sg audio -c "amixer -c $DAC_CARD cset numid=3 -- on; amixer -c $DAC_CARD cset numid=4 -- on; amixer -c $DAC_CARD cset numid=5 -- 192,192; amixer -c $DAC_CARD cset numid=6 -- 192" >/dev/null 2>&1

opened() { for a in 1 2 3 4; do ERR=$(sg audio -c "ffmpeg -v error $1 -f alsa -ac 2 plughw:$DAC_CARD,0" 2>&1); [ -z "$ERR" ] && return 0; sleep 1.5; done; echo "OPENFAIL ${ERR:0:50}"; return 1; }
rec_stream() { ( arecord -q -D hw:$MIC_CARD,0 -f S24_3LE -r 48000 -c 2 -d 4 -t wav $1 >/dev/null 2>&1 & ); sleep 0.4; opened "$2"; sleep 3.4; }

rec_stream $S/dq_decode.wav "-f s32le -ar 176400 -ac 2 -i $S/dp_decode.raw"
echo "== 50%-duty payload (expect FLOOR if DoP-decoded, LOUD if PCM) =="
$PY $S/ears_fft.py $S/dq_decode.wav 1568 2>&1 | grep -E "micL|micR|floor|1568Hz" | head -4

# control: same carrier but 100%-duty payload 0xFF constant -> DSD-full-scale-ish
$PY - <<'EOF'
import struct
S = "/home/ycui/.hermes/cache/scratch"
fs = 176400; nfr = fs * 4
with open(S + "/dp_full.raw", "wb") as f:
    buf = bytearray()
    for fr in range(nfr):
        m = (0x05, 0xFA)[fr & 1]
        word = (m | (0xFF << 8)) | ((m | (0xFF << 8)) << 16)
        buf += struct.pack("<II", word, word)
        if len(buf) >= (1 << 20): f.write(buf); buf = bytearray()
    f.write(buf)
EOF
rec_stream $S/dq_full.wav "-f s32le -ar 176400 -ac 2 -i $S/dp_full.raw"
echo "== all-1s payload (DSD full-scale -> expect loud either way; sanity) =="
$PY $S/ears_fft.py $S/dq_full.wav 1568 2>&1 | grep -E "micL|micR|floor" | head -3
echo ALLDONE
