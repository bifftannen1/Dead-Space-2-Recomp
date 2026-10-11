# Build glue for the checked-in recompiled tree (src/). This is the part of the
# codegen-emitted rexglue.cmake (rexglue-sdk v0.10.0) that a consumer needs, minus the
# codegen step: src/ is the source of truth here, nothing is regenerated at build time.
# The compile options are the ones rexglue.cmake puts on generated code, so the guest
# objects match DS2Recomp's named build byte for byte (outside .debug$ sections).

find_package(rexglue 0.10.0 REQUIRED CONFIG)
message(STATUS "ds2: ReXGlue SDK ${REXGLUE_VERSION_STRING} at ${rexglue_DIR}")

set(REXGLUE_RECOMP_DEBUG_INFO "line-tables-only" CACHE STRING
    "Debug info level for generated code: line-tables-only, full, or none")

# Guest SEH scopes emit __try/__except, which traps hardware faults only under
# asynchronous EH. clang++ spells that flag the GNU way.
set(REXGLUE_RECOMP_OPTIONS "")
if(WIN32)
    if(MSVC)
        list(APPEND REXGLUE_RECOMP_OPTIONS /EHa)
    elseif(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
        list(APPEND REXGLUE_RECOMP_OPTIONS -fasync-exceptions)
    endif()
endif()
if(REXGLUE_RECOMP_DEBUG_INFO STREQUAL "none")
    list(APPEND REXGLUE_RECOMP_OPTIONS $<$<CXX_COMPILER_ID:Clang,AppleClang,GNU>:-g0>)
elseif(REXGLUE_RECOMP_DEBUG_INFO STREQUAL "line-tables-only")
    list(APPEND REXGLUE_RECOMP_OPTIONS $<$<CXX_COMPILER_ID:Clang,AppleClang>:-gline-tables-only>)
endif()

# src/sources.cmake: the 2 support TUs + 157 _units/ TUs. Each unit #includes the original
# source files (DeadSpace/..., EA/..., ...) that hold its functions.
include("${DS2_GEN}/sources.cmake")
set(DS2_GUEST_SOURCES ${GENERATED_SOURCES})
set_source_files_properties(${DS2_GUEST_SOURCES} PROPERTIES COMPILE_OPTIONS "${REXGLUE_RECOMP_OPTIONS}")
list(LENGTH DS2_GUEST_SOURCES _ds2_n)
message(STATUS "ds2: compiling the named tree (${_ds2_n} sources)")

# Usage: ds2_setup_target(<target> [GPU_PLUGINS xenos])
macro(ds2_setup_target target_name)
    add_library(${target_name}_recomp OBJECT ${DS2_GUEST_SOURCES})
    target_include_directories(${target_name}_recomp PRIVATE "${DS2_GEN}" "${DS2_SRC}/app")
    target_link_libraries(${target_name}_recomp PRIVATE rex::runtime)
    rexglue_apply_target_settings(${target_name}_recomp)
    # PCH carries the EH options too: clang rejects a PCH whose EH mode disagrees with the TU.
    target_precompile_headers(${target_name}_recomp PRIVATE "${DS2_GEN}/ds2_rex_diagnostic_pch.h")
    target_compile_options(${target_name}_recomp PRIVATE ${REXGLUE_RECOMP_OPTIONS})
    target_link_libraries(${target_name} PRIVATE ${target_name}_recomp)
    target_include_directories(${target_name} PRIVATE "${DS2_GEN}" "${DS2_SRC}/app")
    target_link_libraries(${target_name} PRIVATE rex::runtime)
    rexglue_configure_target(${target_name} ${ARGN})
endmacro()
