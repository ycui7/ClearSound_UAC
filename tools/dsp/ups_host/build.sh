#!/bin/bash
# Host compile of the EXACT shipping resampler path: csd_ups_block.c (via
# ups_host.c #include, fdhost doctrine) + vendor fd_block_fir.c +
# lib_xcore_math generic-C kernels. Same C as the xcore build, no board file.
#
# Emits the wire stream for a host stream on stdin:
#   printf hostbytes | ups_host --in 192000 --out 384000 > wire.raw
set -u
APP=/home/ycui/sw-audio-sc/sw_usb_audio/wt_bisect
LXM=/home/ycui/sw-audio-sc/lib_xcore_math/lib_xcore_math
LAD=/home/ycui/sw-audio-sc/lib_audio_dsp/lib_audio_dsp
SRC=$APP/tools/dsp/ups_host
S=/home/ycui/.hermes/cache/scratch/ups_host
mkdir -p $S
# feed-owner globals live in csd_dsp.xc (tile[1]); stub them for the host
# link — REVERT with the tap/feed experiment (2026-11-11).
STUB=/tmp/ups_host_feed_stub.c
cat > "$STUB" <<'STUBEOF'
unsigned csd_feed_owner = 0u;
int csd_feed_w0 = 0, csd_feed_w1 = 0;
STUBEOF
REF=$LXM/src/arch/ref
cc -O1 -g -w \
   -I"$LXM/api" -I"$LXM/src/vect" -I"$LXM/src/etc/xmath_fft_lut" \
   -I"$LAD/api" -I"$APP/src/extensions" \
   -o "$S/ups_host" \
   "$SRC/ups_host.c" \
   "$STUB" \
   "$LAD/src/dsp/block_fir/fd_block_fir.c" \
   "$LXM"/src/bfp/*.c \
   "$LXM"/src/fft/fft_bfp.c \
   "$LXM"/src/etc/xmath_fft_lut/*.c \
   $(ls "$LXM"/src/vect/*.c | grep -v mat_mul) \
   "$LXM"/src/scalar/*.c \
   $(find "$REF" -name '*.c' ! -path '*/dct/*' ! -path '*/filter/*' ! -path '*/matrix/*') \
   -lm 2>&1 | grep -E "undefined reference|error:" | head -15
test -x "$S/ups_host" || { echo "CC FAIL"; exit 1; }
echo "CC OK: $S/ups_host"
