#!/bin/bash
# Diagnostic twin of the 16e release image: build recipe byte-identical to
# tools/qualification/build_upsampler.sh EXCEPT -DSPECIALTYCIRCUITS_RELEASE
# removed (restores the full SCPI diag table incl. DIAG:UPSAMPLing:PHASE /
# INFO? / GENerator). Data-path code is untouched by that switch (verified
# 2026-11-27: sites are vendor strings, gen mailbox debug, counters).
# Built from pinned commit 6aec215 in worktree ../wt_diag16e.
cd "$HOME/sw-audio-sc/sw_usb_audio/wt_diag16e" || exit 9
rm -rf build bin .build_2AMi2o2xxxxxd

source "$HOME/XMOS-15.3.1/SetEnv" </dev/null >/dev/null 2>&1 || true
export XMOS_TOOL_PATH="$HOME/XMOS-15.3.1"
export XMOS_HOME="$HOME/.xmos"
export XMOS_CMAKE_PATH="$XMOS_TOOL_PATH/build/xcommon_cmake"
export XCC_DEVICE_PATH="$XMOS_TOOL_PATH/configs:$XMOS_TOOL_PATH/configs/.deprecated"
export XCC_LIBRARY_PATH="$XMOS_TOOL_PATH/target/lib"
export XCC_C_INCLUDE_PATH="$XMOS_TOOL_PATH/target/include:$XMOS_TOOL_PATH/target/include/clang"
export XCC_XC_INCLUDE_PATH="$XMOS_TOOL_PATH/target/include/xc:$XCC_C_INCLUDE_PATH"
export XCC_CPLUS_INCLUDE_PATH="$XCC_C_INCLUDE_PATH:$XMOS_TOOL_PATH/target/include/c++/v1"
export XCC_ASSEMBLER_INCLUDE_PATH="$XCC_C_INCLUDE_PATH"
export XCC_TARGET_PATH="$XMOS_HOME/targets:$XMOS_TOOL_PATH/targets:$XMOS_TOOL_PATH/targets/.deprecated"
export PATH="$XMOS_TOOL_PATH/bin:$PATH"

FLAGS="-DPCM_DSP_EN=1;-DCSD_DIAG_MIDI=1;-DCSD_GEN_EN=1;-DMIXER=1;-DMAX_MIX_COUNT=0;-DCSD_IDLE_POWER;-DCSD_PW_TRACE"
FLAGS="$FLAGS;-DCSD_UPSAMPLE_EN=1;-DCSD_UPS_ASYNC"
if [ -n "${CSD_EXTRA_FLAGS:-}" ]; then FLAGS="$FLAGS;${CSD_EXTRA_FLAGS}"; fi

cmake -B build -DEXTRA_BUILD_FLAGS="$FLAGS" \
  -DXMOS_DEP_DIR_lib_xua="$HOME/sw-audio-sc/lib_xua_diag16e" \
  </dev/null > /tmp/diag_cmake.log 2>&1 \
  || { echo CMAKE-FAIL; tail -20 /tmp/diag_cmake.log; exit 1; }
# A/B provenance: no other tree's path may appear in the generated build files
if grep -rq "sw-audio-sc/sw_usb_audio/clearsound_uac/\|sw-audio-sc/sw_usb_audio/wt_bisect/" build/CMakeFiles 2>/dev/null; then
  grep -rl "sw-audio-sc/sw_usb_audio/clearsound_uac/\|sw-audio-sc/sw_usb_audio/wt_bisect/" build/CMakeFiles | head -3
  echo PROVENANCE-FAIL; exit 1; fi
make -C build -j"$(nproc)" > /tmp/diag_make.log 2>&1 \
  || { echo MAKE-FAIL; grep -aiE 'error|mismatch' /tmp/diag_make.log | head -20; exit 1; }

XE=bin/2AMi2o2xxxxxd/ClearSoundDac_2AMi2o2xxxxxd.xe
cp "$XE" diag16e_$(md5sum "$XE" | cut -c1-8).xe
echo "BUILD-OK $XE"
echo "md5=$(md5sum "$XE" | cut -c1-8)"
