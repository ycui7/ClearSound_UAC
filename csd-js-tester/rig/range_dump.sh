#!/bin/bash
# Payload for run_loaded.sh: read the raw RANGE response with root.
#
# Runs INSIDE the load lifetime, so the RANGE bytes belong to the build the
# loader proved (LIVE BUILD stamp above), not to whatever the EEPROM holds.
set -u
S=/home/ycui/.hermes/cache/scratch
VP=/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac/csd-js-tester/.venv/bin/python
sudo -p 'PW: ' "$VP" "$S/raw_vol_probe6.py"
echo "PROBE_RC=$?"
