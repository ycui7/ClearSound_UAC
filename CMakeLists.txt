cmake_minimum_required(VERSION 3.21)
include($ENV{XMOS_CMAKE_PATH}/xcommon.cmake)
project(ClearSoundDac)

set(APP_HW_TARGET ClearSoundDac.xn)

include(${CMAKE_CURRENT_LIST_DIR}/../deps.cmake)
set(APP_PCA_ENABLE ON)
set(SW_USB_AUDIO_FLAGS ${EXTRA_BUILD_FLAGS} -fcomment-asm
                                            -Wall
                                            -O3
											-report
                                            -lquadflash
                                            -g
                                            -fxscope
                                            -DUSB_TILE=tile[0]
                                            -DADAT_TX_USE_SHARED_BUFF=1
                                            -DXUA_QUAD_SPI_FLASH=1
                                            -DXSCOPE
                                            -DUAC_FORCE_FEEDBACK=1
											-DDEBUG_PRINT_ENABLE=1
                                            -DDSD_CHANS_DAC=2
                                            -DNATIVE_DSD=1
                                            -DSCPI_USER_CONFIG=1)
                                            # -lotp3 DROPPED 2026-11-01: it was
                                            # linked only for csd_identity.xc's
                                            # OTP board-info reads. That module
                                            # is archived in archive_otp/ and
                                            # the unit serial now comes from the
                                            # QSPI die (4Bh), so nothing reads
                                            # OTP. Still no OTP write path
                                            # anywhere: OTP burns 0->1 once, and
                                            # provisioning stays on xburn on the
                                            # production line.
											
											
set(APP_COMPILER_FLAGS_2AMi2o2xxxxxd ${SW_USB_AUDIO_FLAGS})

include(${CMAKE_CURRENT_LIST_DIR}/configs_test.cmake)

set(APP_INCLUDES src src/core src/extensions)
set(XMOS_SANDBOX_DIR ${CMAKE_CURRENT_LIST_DIR}/../..)

# Stamp the build version on every configure. Our build entry configures
# before compiling, so the count only advances on a real build, and the
# header is what carries the version into the binary (IDN + boot banner).
execute_process(
    COMMAND ${CMAKE_CURRENT_LIST_DIR}/tools/gen_version.sh
            ${CMAKE_CURRENT_LIST_DIR}
    OUTPUT_VARIABLE CSD_BUILD_VERSION
    OUTPUT_STRIP_TRAILING_WHITESPACE
)
message(STATUS "ClearSoundDac firmware version: ${CSD_BUILD_VERSION}")

XMOS_REGISTER_APP()
