#!/bin/bash
# Clean build of the firmware with the upsampler active (one block engine,
# two independent clients: the DSP shelf and the sample-rate resampler each
# own their enable -- see csd_dsp.xc / csd_ups_block.c).
#
# Self-contained on purpose: the scratch build_variant.sh this grew out of is
# pruned after 24h, and a build recipe that vanishes is not a build recipe.
#
# FLAGS come from a file, never an inline -D list through the tool layer. An
# inline list delivered '-DCSD_PW_IDLE_MS=<value>"' to xcc -- a literal trailing
# quote, confirmed with od on build/CMakeFiles/*/flags.make -- so the macro
# expanded to a string literal and every use site became a parse error far from
# the cause. The header default CSD_PW_IDLE_MS (300000u) in csd_power.h is the
# same number, so passing nothing changes behaviour.
#
# CSD_UPS_OUT_HZ is deliberately NOT set: the board now derives the wire rate
# from the input's family (csd_ups_expected_out_hz), so one build serves both
# the 48-series -> 384 kHz and the 44.1-series -> 352.8 kHz lanes. Define it to
# pin a bench run to a single family; anything else stays disarmed.
# upsampler arms only at 384000; any other value leaves the DSP path in charge,
# rather than emitting upsampled words at the wrong rate.
#
# A genuinely clean build deletes .build_<config>/: the whole-program analysis
# cached there has previously masked real errors for many builds.
cd "$HOME/sw-audio-sc/sw_usb_audio/clearsound_uac" || exit 9
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

FLAGS="-DPCM_DSP_EN=1;-DCSD_DIAG_MIDI=1;-DCSD_GEN_EN=1;-DMIXER=1;-DMAX_MIX_COUNT=0;-DSPECIALTYCIRCUITS_RELEASE;-DCSD_IDLE_POWER;-DCSD_PW_TRACE"
FLAGS="$FLAGS;-DCSD_UPSAMPLE_EN=1;-DCSD_UPS_ASYNC"
# Optional extra defines from the environment, used by the ADC1 pad-delay
# sweep (~/.hermes/cache/scratch/pad_sweep.sh). Appended here, never passed
# inline through the tool layer, per the warning above.
if [ -n "${CSD_EXTRA_FLAGS:-}" ]; then FLAGS="$FLAGS;${CSD_EXTRA_FLAGS}"; fi

cmake -B build -DEXTRA_BUILD_FLAGS="$FLAGS" </dev/null > /tmp/ups_cmake.log 2>&1 \
  || { echo CMAKE-FAIL; tail -20 /tmp/ups_cmake.log; exit 1; }
make -C build -j"$(nproc)" > /tmp/ups_make.log 2>&1 \
  || { echo MAKE-FAIL; grep -aiE 'error|mismatch' /tmp/ups_make.log | head -20; exit 1; }

XE=bin/2AMi2o2xxxxxd/ClearSoundDac_2AMi2o2xxxxxd.xe
echo "BUILD-OK $XE"
echo "md5=$(md5sum "$XE" | cut -c1-8)"
