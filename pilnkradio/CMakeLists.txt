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

# librtlsdr MUST be the rtl-sdr-blog fork on V4 nodes or they ship ~10 dB deaf
# (see hardening/README on branch sdr-audio-tab). RTLSDR_PREFIX points at the
# fork's install prefix; the rpath bakes it in so no LD_LIBRARY_PATH crutch is
# needed and the startup /proc/self/maps log line will show the fork path.
set(RTLSDR_PREFIX "/usr/local" CACHE PATH "install prefix of the rtl-sdr-blog librtlsdr fork")
find_library(RTLSDR_LIB rtlsdr PATHS ${RTLSDR_PREFIX}/lib NO_DEFAULT_PATH)
if(NOT RTLSDR_LIB)
    message(WARNING "librtlsdr not found under ${RTLSDR_PREFIX}/lib — falling back to system librtlsdr (STOCK DRIVER: V4 dongles will be ~10 dB deaf; dev/CI builds only)")
    find_library(RTLSDR_LIB rtlsdr REQUIRED)
endif()
find_path(RTLSDR_INCLUDE rtl-sdr.h PATHS ${RTLSDR_PREFIX}/include /usr/include)

find_library(FFTW3F_LIB fftw3f REQUIRED)

target_include_directories(pilnkradio PRIVATE ${RTLSDR_INCLUDE})
target_link_libraries(pilnkradio PRIVATE Threads::Threads ${RTLSDR_LIB} ${FFTW3F_LIB})
set_target_properties(pilnkradio PROPERTIES
    BUILD_RPATH  ${RTLSDR_PREFIX}/lib
    INSTALL_RPATH ${RTLSDR_PREFIX}/lib)

install(TARGETS pilnkradio RUNTIME DESTINATION bin)
