# Dead Space 2 for PS5: the recompiled game (src_tu2/, as checked in), the
# ReXGlue runtime and its Xenos GPU backend built as one static PS5 homebrew
# program. Build steps: ps5/BUILD.md.
#
# The PC build (../CMakeLists.txt) links an installed SDK with a runtime DLL
# and a GPU plugin DLL. A PS5 app has no shared libraries of its own, so the
# SDK source (pinned upstream + sdk/rexglue_ds2.patch + sdk/rexglue_ps5.patch,
# put in place by ps5/tools/fetch_sdk.sh) is built here, in this tree.
cmake_minimum_required(VERSION 3.25)
project(ds2-ps5 LANGUAGES C CXX)

if(NOT PS5)
    message(FATAL_ERROR "Configure with -DCMAKE_TOOLCHAIN_FILE=ps5/cmake/ps5-toolchain.cmake (ps5/BUILD.md)")
endif()

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(BUILD_SHARED_LIBS OFF)
# The PS5 SDK's libc++ 18: fmt's compile-time format checks are off, as in the
# GTA IV PS5 port.
add_compile_definitions(FMT_USE_CONSTEVAL=0)

# ThinLTO across the whole program (ps5_package.sh's link finishes it): the
# 47,739 recompiled functions sit in 157 files, and a call from one file to a
# small function in another can't be inlined without it. Off by default: the
# first PS5 builds should be the plain ones the PC build is known by.
option(DS2_PS5_THINLTO "ThinLTO for the PS5 program" OFF)
if(DS2_PS5_THINLTO)
    add_compile_options("$<$<COMPILE_LANGUAGE:C,CXX>:-flto=thin>")
endif()

get_filename_component(DS2_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/.." ABSOLUTE)
set(DS2_SRC "${DS2_ROOT}/src")
set(DS2_GEN "${DS2_ROOT}/src_tu2")
set(DS2_PS5_DIR "${CMAKE_CURRENT_SOURCE_DIR}")
file(STRINGS "${DS2_PS5_DIR}/VERSION.txt" DS2_PS5_VERSION LIMIT_COUNT 1)
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${DS2_PS5_DIR}/VERSION.txt")
message(STATUS "Dead Space 2 for PS5 ${DS2_PS5_VERSION}")

# --- The SDK, built in tree -------------------------------------------------
set(REXSDK_DIR "${DS2_ROOT}/thirdparty/rexglue-sdk-ps5" CACHE PATH "Patched ReXGlue SDK source (ps5/tools/fetch_sdk.sh)")
if(NOT EXISTS "${REXSDK_DIR}/include/rex/ps5_compat.h")
    message(FATAL_ERROR "No PS5-patched ReXGlue SDK at ${REXSDK_DIR}: run ps5/tools/fetch_sdk.sh first")
endif()
set(REXGLUE_USE_VULKAN ON CACHE BOOL "" FORCE)
set(REXGLUE_ENABLE_TRACY OFF CACHE BOOL "" FORCE)
set(REXGLUE_ENABLE_PERF_COUNTERS OFF CACHE BOOL "" FORCE)
set(REXGLUE_ENABLE_FIDELITYFX OFF CACHE BOOL "" FORCE)
set(REXGLUE_BUILD_TESTS OFF CACHE BOOL "" FORCE)
add_subdirectory("${REXSDK_DIR}" rexglue-sdk)

# --- The recompiled game ----------------------------------------------------
# The PC recipe (docs/BUILDING.md): -O2, strict floating point, no strict
# aliasing; the guest code with line tables so a crash address in the log maps
# back to a recompiled function.
set(DS2_OPT "-O2" CACHE STRING "Release optimisation level (-O2 is the PC build's tested default)")
set(CMAKE_CXX_FLAGS_RELEASE "${DS2_OPT} -DNDEBUG")
add_compile_options(-fno-strict-aliasing -ffp-model=strict -fno-char8_t)

include("${DS2_GEN}/sources.cmake")
list(LENGTH GENERATED_SOURCES _ds2_n)
message(STATUS "ds2: compiling the named tree (${_ds2_n} sources)")

add_library(ds2_recomp OBJECT ${GENERATED_SOURCES})
target_include_directories(ds2_recomp PRIVATE "${DS2_GEN}" "${DS2_SRC}/app" "${DS2_PS5_DIR}/src")
target_link_libraries(ds2_recomp PRIVATE rex::runtime)
rexglue_apply_target_settings(ds2_recomp)
target_compile_options(ds2_recomp PRIVATE -gline-tables-only)
# src_tu2/ds2_rex_diagnostic_pch.h with the PS5's guest memory offset after it.
target_precompile_headers(ds2_recomp PRIVATE "${DS2_PS5_DIR}/src/ds2_ps5_pch.h")

# --- The app ----------------------------------------------------------------
add_executable(ds2
    "${DS2_PS5_DIR}/src/main.cpp"
    "${DS2_PS5_DIR}/src/ds2_ps5_install.cpp"
    "${DS2_PS5_DIR}/src/ds2_ps5_disc_swap.cpp"
    # SHA-256 for checking the player's files against the ones the code was made from
    "${REXSDK_DIR}/thirdparty/crypto/sha256.cpp"
)
target_include_directories(ds2 PRIVATE
    "${DS2_GEN}" "${DS2_SRC}/app" "${DS2_PS5_DIR}/src"
    "${REXSDK_DIR}/thirdparty"        # crypto/sha256.h
    "${REXSDK_DIR}/thirdparty/imgui"
)
target_compile_definitions(ds2 PRIVATE DS2_TARGET_TU2=1 DS2_PS5_VERSION="${DS2_PS5_VERSION}")
target_compile_options(ds2 PRIVATE "SHELL:-include ${REXSDK_DIR}/include/rex/ps5_compat.h")
target_compile_options(ds2_recomp PRIVATE "SHELL:-include ${REXSDK_DIR}/include/rex/ps5_compat.h")
# The runtime and the Xenos backend, both static here (the backend is created
# directly by the SDK's gpu_plugin_loader.cpp on the PS5).
target_link_libraries(ds2 PRIVATE
    ds2_recomp
    rex::runtime
    rexgpu-xenos
    SceUserService ScePad SceAudioOut SceSystemService SceVideoOut
)
rexglue_apply_target_settings(ds2)
# The SDK's app template and entry point (rex_app.cpp, windowed_app_main_sdl.cpp).
rexglue_configure_target(ds2)
# CMake's own link is skipped: ps5/tools/ps5_package.sh links the program with
# the PS5 Vulkan driver and start-up code and packages it.
set_property(TARGET ds2 PROPERTY RULE_LAUNCH_LINK "${DS2_PS5_DIR}/tools/ps5_skip_link.sh")
