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
                                            -DBOARD_SUPPORT_BOARD=ClearSoundDac
                                            -DUSB_TILE=tile[0]
                                            -DADAT_TX_USE_SHARED_BUFF=1
                                            -DXUA_QUAD_SPI_FLASH=1
                                            -DXSCOPE
                                            -DUAC_FORCE_FEEDBACK=1
											-DDEBUG_PRINT_ENABLE=1)
											
											
set(APP_COMPILER_FLAGS_2AMi2o2xxxxxx ${SW_USB_AUDIO_FLAGS})

include(${CMAKE_CURRENT_LIST_DIR}/configs_test.cmake)

set(APP_INCLUDES src src/core src/extensions)
set(XMOS_SANDBOX_DIR ${CMAKE_CURRENT_LIST_DIR}/../..)

XMOS_REGISTER_APP()
