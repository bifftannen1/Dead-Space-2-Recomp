# Mod API build glue, included by app/CMakeLists.txt only when DS2_MODS=ON.
#
#   1. tools/modapi/gen_hooks.py resolves the hook names of ../mods/*.toml (PDB name table,
#      cross-checked against the codegen function table) into ${CMAKE_BINARY_DIR}/modapi_gen:
#      ds2_hook_table.h, ds2_hook_dispatch.cpp, mods.cmake. A refused name fails configure.
#   2. src/main.cpp is swapped for src/main_mods.cpp (Ds2ModApp: mod loader + file overlay).
#   3. The dispatchers and every mod's sources (with -DDS2_MOD_ID="<mod>") are added.
#   4. ../mods is staged next to ds2.exe as mods/ (manifests + files roots).
# The generated recomp objects are not touched: the hooks bind at link time (weak -> strong).

find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(DS2_MODS_DIR "${DS2_ROOT}/mods" CACHE PATH "Mods source folder (manifests + sources + files)")
# Per target (DS2_TARGET in CMakeLists.txt): hook-name table at that build's addresses, and for tu2 the
# allocation whose tier=probable rows gen_hooks.py refuses unless a mod sets allow_probable.
if(DS2_TARGET STREQUAL "tu2")
    set(_ds2_names_default "${DS2_ROOT}/tools/modapi/symbols_tu2.tsv")
    set(_ds2_alloc_args --allocation "${DS2_ROOT}/tools/modapi/allocation_tu2.tsv.gz")
else()
    set(_ds2_names_default "${DS2_ROOT}/tools/modapi/symbols_lane5_admitted.tsv")
    set(_ds2_alloc_args)
endif()
if(DS2_NAMES_TSV_OVERRIDE)
    set(DS2_NAMES_TSV "${DS2_NAMES_TSV_OVERRIDE}")
else()
    set(DS2_NAMES_TSV "${_ds2_names_default}")
endif()
set(_ds2_gen "${CMAKE_BINARY_DIR}/modapi_gen")
# The registration table that is compiled: the named tree's (organize --ids pdb renames the
# symbols there, so dispatchers must override the renamed weak symbol) or codegen's.
set(_ds2_reg "${DS2_GEN}/ds2_rex_diagnostic_register.cpp")
set(_ds2_tool "${DS2_ROOT}/tools/modapi/gen_hooks.py")

execute_process(
    COMMAND "${Python3_EXECUTABLE}" "${_ds2_tool}" --mods "${DS2_MODS_DIR}" --names "${DS2_NAMES_TSV}"
            --register "${_ds2_reg}" --out "${_ds2_gen}" ${_ds2_alloc_args}
    RESULT_VARIABLE _ds2_rc OUTPUT_VARIABLE _ds2_out ERROR_VARIABLE _ds2_err)
file(MAKE_DIRECTORY "${_ds2_gen}")
file(WRITE "${_ds2_gen}/gen_hooks.log" "${_ds2_out}${_ds2_err}")
if(NOT _ds2_rc EQUAL 0)
    message(FATAL_ERROR "ds2 mods: gen_hooks.py failed (${_ds2_gen}/gen_hooks.log):\n${_ds2_out}${_ds2_err}")
endif()
message(STATUS "ds2 mods: ${_ds2_out}")
include("${_ds2_gen}/mods.cmake")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    ${DS2_MOD_INPUTS} "${DS2_NAMES_TSV}" "${_ds2_reg}" "${_ds2_tool}")
file(GLOB _ds2_manifests CONFIGURE_DEPENDS "${DS2_MODS_DIR}/*.toml")

# Optional: PDB struct headers (B3). With DS2_TYPES_PDB set to the user's own deadspace_f.pdb,
# tools/modapi/gen_types.py writes ds2_types.h into the build tree (never the source tree);
# mods can then `#include "ds2_types.h"` (test with __has_include).
set(DS2_TYPES_PDB "" CACHE FILEPATH "deadspace_f.pdb for the PDB struct headers (empty: none)")
if(DS2_TYPES_PDB)
    set(_ds2_types "${CMAKE_BINARY_DIR}/types_gen")
    execute_process(
        COMMAND "${Python3_EXECUTABLE}" "${DS2_ROOT}/tools/modapi/gen_types.py"
                --pdb "${DS2_TYPES_PDB}" --out "${_ds2_types}" --sample 200
        RESULT_VARIABLE _ds2_trc OUTPUT_VARIABLE _ds2_tout ERROR_VARIABLE _ds2_terr)
    if(NOT _ds2_trc EQUAL 0)
        message(FATAL_ERROR "ds2 mods: gen_types.py failed:\n${_ds2_tout}${_ds2_terr}")
    endif()
    message(STATUS "ds2 mods: ${_ds2_tout}")
    target_include_directories(ds2_rex_diagnostic PRIVATE "${_ds2_types}")
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${DS2_TYPES_PDB}")
endif()

get_target_property(_ds2_srcs ds2_rex_diagnostic SOURCES)
list(REMOVE_ITEM _ds2_srcs "${DS2_SRC}/app/main.cpp")
set_property(TARGET ds2_rex_diagnostic PROPERTY SOURCES
    ${_ds2_srcs} "${DS2_SRC}/app/main_mods.cpp" "${_ds2_gen}/ds2_hook_dispatch.cpp" ${DS2_MOD_SOURCES})
target_include_directories(ds2_rex_diagnostic PRIVATE
    "${DS2_SRC}/modapi" "${_ds2_gen}")
target_compile_definitions(ds2_rex_diagnostic PRIVATE DS2_MODS=1)

add_custom_command(TARGET ds2_rex_diagnostic POST_BUILD
    COMMAND "${CMAKE_COMMAND}" -E copy_directory "${DS2_MODS_DIR}" "$<TARGET_FILE_DIR:ds2_rex_diagnostic>/mods"
    COMMENT "ds2 mods: staging ${DS2_MODS_DIR} -> mods/")
