#!/bin/bash
# Host compile of the EXACT shipping FIR path: vendor fd_block_fir.c +
# lib_xcore_math generic-C (src/arch/ref) kernels + our csd_dsp_fd.c adapter,
# against the generated csdfd_<fs>.h tables. Same C as the xcore build (no VPU
# asm), no board file, no license, no xsim.
set -u
APP=/home/ycui/sw-audio-sc/sw_usb_audio/clearsound_uac
LXM=/home/ycui/sw-audio-sc/lib_xcore_math/lib_xcore_math
LAD=/home/ycui/sw-audio-sc/lib_audio_dsp/lib_audio_dsp
SRC=$APP/tools/dsp/fdhost
S=/home/ycui/.hermes/cache/scratch/fdhost
mkdir -p $S

REF=$LXM/src/arch/ref
cc -O1 -g -w \
   -DCSD_DSP_DIAG=1 -DCSD_DSP_DIAG_SELFTEST=1 \
   -I"$LXM/api" -I"$LXM/src/vect" -I"$LXM/src/etc/xmath_fft_lut" \
   -I"$LAD/api" -I"$APP/src/extensions" \
   -o "$S/fdtest" \
   "$SRC/host_main.c" \
   "$LAD/src/dsp/block_fir/fd_block_fir.c" \
   "$LXM"/src/bfp/*.c \
   "$LXM"/src/fft/fft_bfp.c \
   "$LXM"/src/etc/xmath_fft_lut/*.c \
   $(ls "$LXM"/src/vect/*.c | grep -v mat_mul) \
   "$LXM"/src/scalar/*.c \
   $(find "$REF" -name '*.c' ! -path '*/dct/*' ! -path '*/filter/*' ! -path '*/matrix/*') \
   -lm 2>&1 | grep -E "undefined reference|error:" | head -15
test -x "$S/fdtest" || { echo "CC FAIL"; exit 1; }
echo "CC OK"
"$S/fdtest" 48000  1000 0.5 4096
"$S/fdtest" 48000  60   0.5 4096
"$S/fdtest" 48000  1500 0.5 4096
"$S/fdtest" 384000 1000 0.5 8192
