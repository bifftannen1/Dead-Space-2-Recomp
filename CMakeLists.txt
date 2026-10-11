# Dead Space 2 (Xbox 360) - ReXGlue static recompilation, PC build, from checked-in source.
#
# src/ holds the recompiled C++ (named tree, real PDB identifiers) and is the source of
# truth: this project does not run rexglue codegen or the organizer. The SDK is the pinned
# upstream commit + sdk/rexglue_ds2.patch, built by tools/fetch_sdk.py; point
# CMAKE_PREFIX_PATH at its install prefix. See docs/BUILDING.md.
#
# The target name must stay ds2_rex_diagnostic: it is the manifest's [project] name that
# REX_DEFINE_APP and the generated init/register files were emitted against.

cmake_minimum_required(VERSION 3.25)
project(ds2_rex_diagnostic LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# The working PC build recipe: -O2 instead of the SDK's Release -O3, plus the SDK's global
# flags, which an installed SDK package does not carry to consumers.
set(CMAKE_CXX_FLAGS_RELEASE "-O2 -DNDEBUG")
add_compile_options(-fno-strict-aliasing -ffp-model=strict -fno-char8_t)

set(DS2_ROOT "${CMAKE_CURRENT_SOURCE_DIR}")
set(DS2_SRC "${CMAKE_CURRENT_SOURCE_DIR}/src")
# DS2_TARGET: base = src/ (retail Disc 1 XEX), tu2 = src_tu2/ (the same XEX with Title Update #2).
# src/app and src/modapi are shared; only the recompiled tree differs.
set(DS2_TARGET "base" CACHE STRING "base or tu2")
set_property(CACHE DS2_TARGET PROPERTY STRINGS base tu2)
if(DS2_TARGET STREQUAL "tu2")
    set(DS2_GEN "${CMAKE_CURRENT_SOURCE_DIR}/src_tu2")
elseif(DS2_TARGET STREQUAL "base")
    set(DS2_GEN "${DS2_SRC}")
else()
    message(FATAL_ERROR "DS2_TARGET must be base or tu2")
endif()
message(STATUS "ds2: target ${DS2_TARGET} (${DS2_GEN})")
include(cmake/ds2_recomp.cmake)

add_executable(ds2_rex_diagnostic WIN32 "${DS2_SRC}/app/main.cpp")
set_target_properties(ds2_rex_diagnostic PROPERTIES OUTPUT_NAME ds2)

# GPU_PLUGINS stages rexgpu-xenos.dll (built from the patched SDK, so it carries the C4
# compute-write barrier fix) next to ds2.exe; ds2_app.h defaults gpu_plugin=xenos.
ds2_setup_target(ds2_rex_diagnostic GPU_PLUGINS xenos)
if(DS2_TARGET STREQUAL "tu2")
    target_compile_definitions(ds2_rex_diagnostic PRIVATE DS2_TARGET_TU2=1)
endif()

# Mod API (docs/MODDING.md). OFF: nothing below is compiled and the app is the plain build.
option(DS2_MODS "Build with the mod API, the mods in mods/ and the mod loader" OFF)
if(DS2_MODS)
    set(DS2_NAMED_TREE ON)
    include(src/modapi/modapi.cmake)
endif()
