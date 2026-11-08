#!/bin/bash
# Qualify the upsampling filter math on the XC simulator against the tester
# math executed on this server, and fail loudly if the two disagree.
#
#   tester math : gcc over the same sources (this machine)
#   device math : xcc -target=XCORE-AI-EXPLORER, run under xrun (the XMOS
#                 simulator), i.e. XS3 code generation of the shipped core
#
# Both sides compile the SAME translation units, so a difference can only come
# from the toolchain's arithmetic, not from the test.
#
# Output plumbing: a C .xe on XS3 produces no stdout, and the XC harness cannot
# link against the C core because xcc's cross-language type check rejects the
# pair (C int64_t is 'long', XC's is 'long long'). So the report is buffered in
# the C side and printed by a thin XC wrapper via printchar under --xscope.
# Nothing wider than one char crosses that boundary.
#
# Exit status: 0 only when the simulator says SIM-PASS and its report is
# byte-identical to the host report.
set -u
# The script lives at tools/dsp/xc_sim/, three levels below the repo root.
cd "$(dirname "$0")/../../.." || exit 9

echo "### tester math on this server (gcc)"
rm -f /tmp/csd_sim_host
gcc -O2 -Wall -x c -Isrc/dsp tools/dsp/xc_sim/csd_ups_sim.xc src/dsp/csd_upsample.c -lm \
    -o /tmp/csd_sim_host 2>&1 | grep -a ' error' | head -5
[ -x /tmp/csd_sim_host ] || { echo "HOST-BUILD-FAILED"; exit 1; }
/tmp/csd_sim_host > /tmp/csd_sim_host.txt || { echo "HOST-RUN-FAILED"; exit 1; }
cat /tmp/csd_sim_host.txt

# set +u around SetEnv: it references variables the caller has not set, and it
# exits rather than returning, which "|| true" cannot catch. With set -u active
# it kills this script silently at that line -- traced with bash -x.
set +u
source "$HOME/XMOS/XTC/15.3.0/SetEnv" </dev/null >/dev/null 2>&1 || true
set -u
export XMOS_TOOL_PATH="$HOME/XMOS/XTC/15.3.0"; export XMOS_HOME="$HOME/.xmos"
export XMOS_CMAKE_PATH="$XMOS_TOOL_PATH/build/xcommon_cmake"
export XCC_DEVICE_PATH="$XMOS_TOOL_PATH/configs:$XMOS_TOOL_PATH/configs/.deprecated"
export XCC_LIBRARY_PATH="$XMOS_TOOL_PATH/target/lib"
export XCC_C_INCLUDE_PATH="$XMOS_TOOL_PATH/target/include:$XMOS_TOOL_PATH/target/include/clang"
export XCC_XC_INCLUDE_PATH="$XMOS_TOOL_PATH/target/include/xc:$XCC_C_INCLUDE_PATH"
export XCC_CPLUS_INCLUDE_PATH="$XCC_C_INCLUDE_PATH:$XMOS_TOOL_PATH/target/include/c++/v1"
export XCC_ASSEMBLER_INCLUDE_PATH="$XCC_C_INCLUDE_PATH"
export XCC_TARGET_PATH="$XMOS_HOME/targets:$XMOS_TOOL_PATH/targets:$XMOS_TOOL_PATH/targets/.deprecated"
export PATH="$XMOS_TOOL_PATH/bin:$PATH"
T=XCORE-AI-EXPLORER
rm -f /tmp/csd_core.o /tmp/csd_harness.o /tmp/csd_wrapper.o /tmp/csd_sim.xe

echo "### XS3 build (XMOS toolchain)"
# Each source is compiled separately: xcc's driver loses the path of a .c given
# alongside .xc on one command line.
xcc -O2 -target=$T -fxscope -Isrc/dsp -c src/dsp/csd_upsample.c -o /tmp/csd_core.o 2>&1 | grep -a ' error' | head -3
xcc -O2 -target=$T -fxscope -x c -DCSD_SIM_STORE -Isrc/dsp -c tools/dsp/xc_sim/csd_ups_sim.xc -o /tmp/csd_harness.o 2>&1 | grep -a ' error' | head -3
xcc -O2 -target=$T -fxscope -Isrc/dsp -c tools/dsp/xc_sim/sim_main.xc -o /tmp/csd_wrapper.o 2>&1 | grep -a ' error' | head -3
for o in /tmp/csd_core.o /tmp/csd_harness.o /tmp/csd_wrapper.o; do
  [ -f "$o" ] || { echo "MISSING $o"; exit 1; }
done
xcc -O2 -target=$T -fxscope /tmp/csd_wrapper.o /tmp/csd_harness.o /tmp/csd_core.o -lxscope \
    -o /tmp/csd_sim.xe 2>&1 | grep -a ' error' | head -3
[ -f /tmp/csd_sim.xe ] || { echo "XSIM-BUILD-FAILED"; exit 1; }

echo "### XS3 simulator (xrun/xsim)"
timeout 900 xrun --xscope /tmp/csd_sim.xe > /tmp/csd_sim_xsim.txt 2>&1
rc=$?
[ $rc -eq 0 ] || { echo "XRUN-FAILED rc=$rc"; head -5 /tmp/csd_sim_xsim.txt; exit 1; }
cat /tmp/csd_sim_xsim.txt

echo "### verdict"
grep -q 'SIM-PASS' /tmp/csd_sim_xsim.txt || { echo "SIM-REPORTED-FAIL"; exit 1; }
if diff /tmp/csd_sim_host.txt /tmp/csd_sim_xsim.txt; then
  echo "IDENTICAL: XS3 simulator == tester math ($(wc -l < /tmp/csd_sim_host.txt) lines, all hashes equal)"
  exit 0
fi
echo "DIFFERS: XS3 codegen disagrees with the tester math"
exit 1
