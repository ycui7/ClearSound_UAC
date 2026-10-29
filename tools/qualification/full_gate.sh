#!/bin/bash
# One shell: RAM-load the build, then run mute gate AND volume gate before the
# load dies. This is the full audio verification for a unit.
set -u
S=/home/ycui/.hermes/cache/scratch
export HOME=/home/ycui
export XMOS_LICENSE_FILE=/home/ycui/workspace/xmos-installer/xTime_key.txt
PY=/home/ycui/.hermes/hermes-agent/venv/bin/python

cd ~/sw-audio-sc/sw_usb_audio/clearsound_uac
( cd ~/XMOS/XTC/15.3.0 && set +u && source ./SetEnv >/dev/null 2>&1
  cd ~/sw-audio-sc/sw_usb_audio/clearsound_uac && exec xrun bin/2AMi2o2xxxxxd/ClearSoundDac_2AMi2o2xxxxxd.xe ) >/dev/null 2>&1 &
LOAD=$!
trap 'kill $LOAD 2>/dev/null' EXIT
for i in $(seq 1 30); do grep -q XMOS /proc/asound/cards && break; sleep 1; done
grep -q XMOS /proc/asound/cards || { echo "CARD-FAIL"; exit 1; }
sleep 2
echo "== firmware loaded =="

bash $S/mute_gate.sh
bash $S/vol_gate.sh $1
kill $LOAD 2>/dev/null; trap - EXIT
echo "== verifying captures against the LOADED firmware =="
for f in $S/m_on.wav $S/m_off.wav $S/m_back.wav $S/vol_${1}_full.wav $S/vol_${1}_half.wav; do
    printf "%-28s " "$(basename $f)"
    $PY $S/ears_fft.py $f 1000 2>&1 | grep -oE "1000Hz: [-+0-9.]+dBFS" | tr '\n' ' '; echo
done
