# CMake toolchain for building Dead Space 2 as a native PS5 homebrew app
# (the whole command: ps5/BUILD.md). After the GTA IV PS5 port's toolchain file.
#
#   cmake -S ps5 -B out/ps5 -G Ninja \
#     -DCMAKE_TOOLCHAIN_FILE=ps5/cmake/ps5-toolchain.cmake \
#     -DPS5_PAYLOAD_SDK=/path/to/ps5-payload-sdk
#
# Uses the PS5 payload SDK (mihawk-99's fork, as ps5-plume-triangle installs
# it) for clang, headers, libc++ 18 and the kernel stubs.

if(NOT PS5_PAYLOAD_SDK)
  set(PS5_PAYLOAD_SDK "$ENV{PS5_PAYLOAD_SDK}")
endif()
if(NOT PS5_PAYLOAD_SDK OR NOT EXISTS "${PS5_PAYLOAD_SDK}/toolchain/prospero.cmake")
  message(FATAL_ERROR "Set PS5_PAYLOAD_SDK to the PS5 payload SDK root "
                      "(ps5-plume-triangle/.deps/native/ps5-payload-sdk)")
endif()
# Pass it through to try_compile projects.
list(APPEND CMAKE_TRY_COMPILE_PLATFORM_VARIABLES PS5_PAYLOAD_SDK)

include("${PS5_PAYLOAD_SDK}/toolchain/prospero.cmake")
set(CMAKE_VERBOSE_MAKEFILE OFF CACHE BOOL "" FORCE)

# Zen 2 (the PS5 CPU); emulated TLS (the PS5 ABI); libc++ 18 needs
# -fexperimental-library for std::jthread / stop_token; the PS5 target has
# exceptions and RTTI off unless asked for. Linux names are mapped to FreeBSD
# ones by rex/ps5_compat.h, which the SDK force-includes.
set(PS5_COMMON_FLAGS
  "-march=znver2 -femulated-tls -fno-stack-protector -fno-plt -fdenormal-fp-math=ieee -fvisibility-nodllstorageclass=default")
set(CMAKE_C_FLAGS_INIT "${PS5_COMMON_FLAGS}")
set(CMAKE_CXX_FLAGS_INIT "${PS5_COMMON_FLAGS} -fexperimental-library -frtti -fexceptions")
set(CMAKE_EXE_LINKER_FLAGS_INIT "-fexperimental-library")
