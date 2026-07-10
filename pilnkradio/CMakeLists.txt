cmake_minimum_required(VERSION 3.13)
project(pilnkradio CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

add_executable(pilnkradio main.cpp)
target_compile_options(pilnkradio PRIVATE -Wall -Wextra -O2)

find_package(Threads REQUIRED)
target_link_libraries(pilnkradio PRIVATE Threads::Threads)

# Milestone 2+ (uncomment when the RTL/FFT path lands):
#   librtlsdr MUST be the rtl-sdr-blog fork or every V4 node ships ~10 dB deaf
#   (see hardening/README on branch sdr-audio-tab). Link it explicitly with an
#   rpath to /usr/local/lib so no LD_LIBRARY_PATH crutch is needed.
# find_library(RTLSDR_LIB rtlsdr PATHS /usr/local/lib NO_DEFAULT_PATH)
# find_library(FFTW3F_LIB fftw3f)
# target_link_libraries(pilnkradio PRIVATE ${RTLSDR_LIB} ${FFTW3F_LIB})
# set_target_properties(pilnkradio PROPERTIES BUILD_RPATH /usr/local/lib INSTALL_RPATH /usr/local/lib)

install(TARGETS pilnkradio RUNTIME DESTINATION bin)
