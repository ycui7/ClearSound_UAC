#!/bin/bash
# Serve the bench console.
#
# Both WebUSB and Web MIDI require a *secure context*. Loopback counts as one
# -- both http://localhost and http://127.0.0.1 -- so no certificate is needed
# as long as the page is opened from this machine. A LAN IP or hostname is NOT
# a secure context, where navigator.usb and navigator.requestMIDIAccess are
# simply absent; that reads as a broken app rather than a URL problem, so this
# script prints the URLs that work and binds to loopback only.
set -eu
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-8765}"

if ! command -v python3 >/dev/null; then
  echo "python3 is required to serve the console" >&2; exit 1
fi

echo "ClearSoundDac bench console"
echo "  open   http://localhost:${PORT}/"
echo "  Chrome also accepts http://127.0.0.1:${PORT}/ ; Firefox and Safari"
echo "  honour only the localhost hostname, so prefer localhost."
echo "  A LAN address is NOT a secure context: navigator.usb and"
echo "  navigator.requestMIDIAccess are undefined there and the page cannot"
echo "  reach the board at all. Serve over TLS if it must be remote."
echo
echo "Before connecting, make sure nothing else holds the board:"
echo "  amidi -l                     # confirm the MIDI port is enumerated"
echo "  fuser -v /dev/snd/midiC*D*   # another app grabbing it blocks replies"
echo "If you choose the WebUSB path, the kernel driver must release the MIDI"
echo "interface first -- the console prints the exact unbind command when the"
echo "claim fails. The Web MIDI path needs no unbind."
echo
cd "$DIR"
exec python3 -m http.server "$PORT" --bind 127.0.0.1
