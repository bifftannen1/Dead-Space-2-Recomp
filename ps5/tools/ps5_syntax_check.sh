#!/usr/bin/env bash
# Dead Space 2 for PS5: a syntax-only compile of every source in the PS5 build,
# for when the PS5 toolchain isn't built (or to check a change quickly).
#
#   ps5/tools/ps5_syntax_check.sh <ps5 headers> [jobs]
#
# <ps5 headers> is a directory with target/include in it: an installed PS5
# payload SDK, or the headers of its source tree copied together:
#
#   mkdir -p H/target/include/ps5
#   cp -r PS5_PayloadSDK/include/freebsd/. PS5_PayloadSDK/include/khronos/. H/target/include/
#   cp -r PS5_PayloadSDK/include/ps5/. H/target/include/ps5/
#   cp -r PS5_PayloadSDK/platform/include/ps5platform H/target/include/
#
# The C++ library headers are the SDK's own (target/include/c++/v1) when it has
# them, else the host's libc++ (apt install libc++-18-dev). The SDK source is
# thirdparty/rexglue-sdk-ps5 (ps5/tools/fetch_sdk.sh) or $REXSDK_DIR.
#
# This shows the code parses and type-checks for x86_64-sie-ps5. It doesn't
# compile the 157 recompiled units (the PC build already does, from the same
# files), and it says nothing about linking or running.
#
# PS5_CHECK_OBJ_DIR=<dir> compiles to PS5 object files in <dir> instead (-O0):
# slower, but it also runs the assembler and catches code that needs CPU
# features the PS5 doesn't have.
set -uo pipefail

headers=$(realpath "${1:?directory with target/include (see the top of this script)}")
jobs=${2:-$(nproc 2>/dev/null || echo 4)}
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
sdk=${REXSDK_DIR:-$repo/thirdparty/rexglue-sdk-ps5}
[[ -f $sdk/include/rex/ps5_compat.h ]] || { echo "no PS5-patched SDK at $sdk (ps5/tools/fetch_sdk.sh)" >&2; exit 2; }
[[ -d $headers/target/include ]] || { echo "$headers has no target/include" >&2; exit 2; }
cxx=${CXX:-$(command -v clang++-19 || command -v clang++-18 || command -v clang++)}
cc=${CC:-$(command -v clang-19 || command -v clang-18 || command -v clang)}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/include/rex"
sed -e 's/@REXGLUE_DERIVED_MAJOR@/0/;s/@REXGLUE_DERIVED_MINOR@/10/;s/@REXGLUE_DERIVED_PATCH@/0/' \
    -e 's/@REXGLUE_DERIVED_TWEAK@/0/;s/@[A-Z_]*VERSION[A-Z_]*@/0.10.0/g;s/@REX_PLATFORM@/ps5-amd64/' \
    -e 's/@REXGLUE_BUILD_TIMESTAMP@/0/' "$sdk/include/rex/version.h.in" > "$work/include/rex/version.h"

if [[ -d $headers/target/include/c++/v1 ]]; then
    libcxx=$headers/target/include/c++/v1
else
    libcxx=$(ls -d /usr/lib/llvm-*/include/c++/v1 2>/dev/null | sort -V | tail -1)
fi
[[ -n $libcxx && -d $libcxx ]] || { echo "no libc++ headers (apt install libc++-18-dev)" >&2; exit 2; }

t=$sdk/thirdparty
flags=(--target=x86_64-sie-ps5 -march=znver2 -femulated-tls -fno-stack-protector -fno-plt
    -fdenormal-fp-math=ieee -fvisibility-nodllstorageclass=default
    -isysroot "$headers" -isystem "$headers/target/include"
    -fno-strict-aliasing -Wno-everything -ferror-limit=8
    -DREX_PLATFORM_LINUX=1 -DREX_PLATFORM_PS5=1 -DREX_HAS_VULKAN=1 '-DREXGLUE_BUILD_CONFIG="Release"'
    -DNDEBUG -DSPDLOG_FMT_EXTERNAL -DSPDLOG_COMPILED_LIB -DFMT_USE_CONSTEVAL=0 -DVK_NO_PROTOTYPES
    -DDS2_TARGET_TU2=1 '-DDS2_PS5_VERSION="check"'
    -include "$sdk/include/rex/ps5_compat.h"
    -I"$sdk/include" -I"$work/include" -I"$sdk" -I"$sdk/src" -I"$t"
    -I"$repo/src_tu2" -I"$repo/src/app" -I"$repo/ps5/src"
    -isystem "$t/fmt/include" -isystem "$t/spdlog/include" -isystem "$t/simde" -isystem "$t/simde/simde"
    -isystem "$t/tomlplusplus/include" -isystem "$t/sdl3/include" -isystem "$t/vulkan-headers/include"
    -isystem "$t/vulkan-memory-allocator/include" -isystem "$t/imgui" -isystem "$t/utfcpp/source"
    -isystem "$t/xxHash" -isystem "$t/cli11/include" -isystem "$t/disruptorplus/include"
    -isystem "$t/glslang" -isystem "$t/spirv-headers/include" -isystem "$t/spirv-tools/include"
    -isystem "$t/FFmpeg" -isystem "$t/renderdoc" -isystem "$t/stb" -isystem "$t/o1heap/o1heap"
    -isystem "$t/libmspack/cabextract/mspack")
cxxflags=(-std=c++23 -nostdinc++ -isystem "$libcxx" -fexperimental-library -frtti -fexceptions -fno-char8_t)

# The sources the PS5 build compiles: what the SDK's CMake files list, less the
# other platforms' and D3D12's, plus the PS5 files and the app.
list=$work/files
{
    for f in arena bit_map bit_stream byte_stream clock cvar exception_handler filesystem \
             filesystem_wildcard hash logging memory mutex ring_buffer string_buffer threading \
             timer_queue utf8 vec128 atomic_posix clock_posix dbg_posix dynlib_posix \
             exception_handler_posix filesystem_posix fiber_posix mapped_memory_posix math_gcc \
             memory_posix platform/console_posix platform/env_posix seh_posix socket_posix \
             system_posix threading_posix ps5_diag; do echo "$sdk/src/core/$f.cpp"; done
    echo "$sdk/src/core/ps5_libc_extras.c"
    for d in filesystem ui input audio graphics system kernel; do
        grep -oE '^[[:space:]]+[A-Za-z0-9_/.-]+\.cpp[[:space:]]*$' "$sdk/src/$d/CMakeLists.txt" |
            tr -d ' \t' | grep -vE 'd3d12|dxbc|surface_(win|mac|gnulinux)|moltenvk|_nx\.cpp' |
            sed "s|^|$sdk/src/$d/|"
    done
    for f in graphics/xenos graphics/pipeline/texture/info graphics/pipeline/texture/info_formats \
             graphics/pipeline/texture/extent graphics/pipeline/texture/util \
             graphics/pipeline/texture/conversion ui/surface_ps5 ui/rex_app ui/windowed_app_main_sdl \
             input/ps5/ps5_input_driver input/sdl/sdl_input_driver audio/ps5/ps5_audio_driver; do
        echo "$sdk/src/$f.cpp"
    done
    for f in main ds2_ps5_install ds2_ps5_disc_swap; do echo "$repo/ps5/src/$f.cpp"; done
} | awk '!seen[$0]++' > "$list"

objdir=${PS5_CHECK_OBJ_DIR:-}
[[ -z $objdir ]] || { mkdir -p "$objdir" && objdir=$(realpath "$objdir"); }

check() {
    local f=$1 n=$2 out mode=(-fsyntax-only)
    [[ -f $f ]] || { echo "MISSING $f"; return 1; }
    if [[ -n $objdir ]]; then
        mode=(-c -O0 -o "$objdir/$(printf '%03d' "$n")_$(basename "${f%.*}").o")
    fi
    if [[ $f == *.c ]]; then
        out=$("$cc" "${flags[@]}" "${mode[@]}" "$f" 2>&1)
    else
        out=$("$cxx" "${cxxflags[@]}" "${flags[@]}" "${mode[@]}" "$f" 2>&1)
    fi
    if [[ -n $out ]]; then
        echo "FAIL $f"
        echo "$out" | grep -E 'error|fatal' | head -8
        return 1
    fi
    echo "ok   ${f#"$repo"/}"
}

total=$(wc -l < "$list")
echo "==> $total sources, $jobs at a time ($cxx, libc++ headers $libcxx)"
pids=() fail=0 n=0
while IFS= read -r f; do
    check "$f" "$n" > "$work/out.$n" 2>&1 &
    pids+=($!)
    n=$((n + 1))
    if (( ${#pids[@]} >= jobs )); then
        wait "${pids[0]}" || fail=$((fail + 1))
        pids=("${pids[@]:1}")
    fi
done < "$list"
for p in "${pids[@]}"; do wait "$p" || fail=$((fail + 1)); done
for ((i = 0; i < n; i++)); do grep -v '^ok ' "$work/out.$i"; done
echo "==> $((total - fail)) of $total pass"
(( fail == 0 ))
