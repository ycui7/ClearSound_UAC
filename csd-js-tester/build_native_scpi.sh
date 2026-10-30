#!/bin/bash
# Compile the REAL SCPI personality + REAL libscpi for the host and run the
# harness. No board, no XTC, seconds to run.
#
# Why not the Python --sim backend: it is a second implementation of the
# device's behaviour, so it agrees with itself, not with the firmware. The bug
# under investigation ("DIAG:DSP:PRESet 0" accepted, state never changes) is
# precisely the kind that a parallel model hides: if the pattern never matches,
# the real device answers -113 and the sim answers whatever the sim author
# assumed.
#
# Same source files as the xcore build (see libscpi module's SOURCES), same
# -DSCPI_USER_CONFIG=1, so the parser configuration matches the device image.
set -eu
APP=/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac
LSCPI=/home/ycui/sw-audio-sc/libscpi/libscpi
OUT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
S=$HOME/.hermes/cache/scratch

# lib_i2c headers are needed only by csd_diag_regs.xc (not compiled here), but
# the personality's include chain reaches the app's extension headers, so keep
# the same -I set the firmware uses for that directory.
INC="-I$APP/src/extensions -I$LSCPI/inc -I$APP/src/core"

# fifo_* collide with lib_dfu in the xcore build via -D renames; irrelevant on
# the host, where nothing else defines them, so no renames here.
rm -f "$OUT/native_scpi_harness"
# Do NOT pipe the compiler through grep/head: the pipeline's exit status would
# be head's (0), and the run below would then execute whatever binary was left
# from a previous invocation -- stale output presented as fresh results. This
# bit once already, when a table-entry rename failed to compile and the old
# binary kept printing the pre-fix answers.
gcc -O0 -g -DSCPI_USER_CONFIG=1 $INC \
    "$OUT/native_scpi_harness.c" \
    "$APP/src/extensions/csd_diag_scpi.c" \
    $LSCPI/src/{error,expression,fifo,ieee488,lexer,minimal,parser,units,utils}.c \
    -o "$OUT/native_scpi_harness"
gcc_rc=$?

if [ $gcc_rc -ne 0 ] || [ ! -x "$OUT/native_scpi_harness" ]; then
    echo "BUILD FAILED (rc=$gcc_rc) -- refusing to run a stale binary"
    exit 1
fi

echo "== native personality =="
"$OUT/native_scpi_harness"