cmake_minimum_required(VERSION 3.13)
project(pilnk_bridge)

file(GLOB SRC "src/*.cpp")

include(${SDRPP_MODULE_CMAKE})

target_include_directories(pilnk_bridge PRIVATE "src/")
# Absolute (symlink-proof) path to the radio module's interface header.
# This module's source is canonical in the PiLNK repo and the SDR++ build
# tree symlinks to it, so relative "../../" paths would resolve wrongly.
target_include_directories(pilnk_bridge PRIVATE "${CMAKE_SOURCE_DIR}/decoder_modules/radio/src")
# RTL-SDR source dir for the shared rtl_sdr_interface.h (RF/tuner gain control)
target_include_directories(pilnk_bridge PRIVATE "${CMAKE_SOURCE_DIR}/source_modules/rtl_sdr_source/src")

# FFTW (single-precision) for the FFT tap
find_package(PkgConfig)
pkg_check_modules(FFTW3F REQUIRED fftw3f)
target_include_directories(pilnk_bridge PRIVATE ${FFTW3F_INCLUDE_DIRS})
target_link_directories(pilnk_bridge PRIVATE ${FFTW3F_LIBRARY_DIRS})
target_link_libraries(pilnk_bridge PRIVATE ${FFTW3F_LIBRARIES})
