# Scoped configure-time source fingerprint. No full-tree/binary correspondence claim.
set(_ds2_provenance_inputs
    src/app/ds2_app.h src/app/ds2_startup_provenance.h src/app/ds2_cadence_window.h
    src/app/main.cpp src/app/main_mods.cpp CMakeLists.txt cmake/ds2_provenance.cmake
    sdk/REXGLUE_BASE.txt sdk/rexglue_ds2.patch
    sdk-build-source/src/core/cvar.cpp sdk-build-source/include/rex/cvar.h
    sdk-build-source/src/core/clock.cpp sdk-build-source/src/ui/rex_app.cpp
    sdk-build-source/src/system/gpu_plugin_loader.cpp
    sdk-build-source/src/ui/vulkan/vulkan_presenter.cpp
    sdk-build-source/src/ui/vulkan/vulkan_device.cpp sdk-build-source/src/ui/window_sdl.cpp
    sdk-build-source/src/graphics/vulkan/pipeline_cache.cpp
    profiles/claude60/mods/framerate/framerate.cpp)
set(_ds2_provenance_text "")
foreach(_path IN LISTS _ds2_provenance_inputs)
    set(_absolute "${DS2_ROOT}/${_path}")
    if(EXISTS "${_absolute}")
        file(SHA256 "${_absolute}" _sha)
        set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${_absolute}")
    else()
        set(_sha "missing")
    endif()
    string(APPEND _ds2_provenance_text "${_path}=${_sha};")
endforeach()
file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/ds2-provenance")
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/ds2-provenance/ds2_build_provenance.h"
    "#pragma once\nnamespace ds2::provenance { inline constexpr const char* kBuildSources = \"${_ds2_provenance_text}\"; }\n")
target_include_directories(ds2_rex_diagnostic PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/ds2-provenance")
target_link_libraries(ds2_rex_diagnostic PRIVATE bcrypt shell32)
