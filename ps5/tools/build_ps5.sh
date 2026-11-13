#!/usr/bin/env bash
# Dead Space 2 for PS5: from a clean checkout to a zip of the app, one command.
#
#   ps5/tools/build_ps5.sh [step ...]        (from the repository root)
#
# Steps, in this order; with no argument, all of them:
#
#   toolchain   Nazky's ps5-plume-triangle at the commit pinned below, built
#               with its own tools/build.sh: the PS5 payload SDK, the PS5
#               Vulkan driver (RADV), the app start-up code and the signer.
#               It pins the SDK and driver commits itself.
#   sdk         ps5/tools/fetch_sdk.sh: the patched ReXGlue SDK
#   compile     CMake and ninja (the long one: 852 steps)
#   package     ps5/tools/ps5_package.sh: link, sign, the app folder
#   zip         out/ps5/DeadSpace2-PS5-<version>.zip
#
# Each step writes out/ps5/logs/<step>.log and prints the end of it if it
# fails. A step that is already done is quick to repeat, so after a fix run
# the failed step and the ones after it (e.g. "compile package zip").
#
# This is the command .claude/settings.json allows in a Claude Code cloud
# session, where the toolchain's build scripts are otherwise refused as code
# from outside the repository. Run it as written above, with no pipe or
# redirection around it: the logs are already in files.
#
#   DS2_PS5_TRI     the ps5-plume-triangle checkout (thirdparty/ps5-plume-triangle)
#   DS2_PS5_BUILD   the CMake build directory (out/ps5)
#   DS2_PS5_JOBS    compiler jobs for ninja (its default: one per core)
set -uo pipefail

tri_url=https://github.com/Nazky/ps5-plume-triangle
tri_rev=d54d9adb084bc038023ec4d8bbd8a4ba23f5880d

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
tri=${DS2_PS5_TRI:-$repo/thirdparty/ps5-plume-triangle}
build=${DS2_PS5_BUILD:-$repo/out/ps5}
logs=$build/logs
version=$(head -1 "$repo/ps5/VERSION.txt")
all_steps=(toolchain sdk compile package zip)

steps=("$@")
[[ ${#steps[@]} -gt 0 ]] || steps=("${all_steps[@]}")
for s in "${steps[@]}"; do
    case $s in
        toolchain|sdk|compile|package|zip) ;;
        *) echo "usage: ps5/tools/build_ps5.sh [${all_steps[*]}]" >&2; exit 2 ;;
    esac
done

check_tools() {
    local missing=() c
    for c in git wget curl unzip xz tar make sha256sum rsync python3 cmake ninja meson \
             glslangValidator bison flex gcc g++ pkg-config; do
        command -v "$c" > /dev/null || missing+=("$c")
    done
    command -v clang-19 > /dev/null || command -v clang-18 > /dev/null || command -v clang > /dev/null ||
        missing+=("clang (18 or 19)")
    command -v ld.lld > /dev/null || command -v ld.lld-19 > /dev/null || command -v ld.lld-18 > /dev/null ||
        missing+=(lld)
    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "missing on this machine: ${missing[*]}" >&2
        echo "Ubuntu: apt-get install git wget curl unzip xz-utils make rsync python3 cmake ninja-build meson" \
             "glslang-tools bison flex gcc g++ pkg-config clang-18 lld-18 python3-mako python3-ply python3-yaml" >&2
        return 1
    fi
    # The pinned PS5_Mesa needs meson 1.4 or later; Ubuntu 24.04's is 1.3.2.
    local mv
    mv=$(meson --version)
    if ! printf '1.4.0\n%s\n' "$mv" | sort -VC; then
        echo "meson $mv is too old: the PS5 Vulkan driver (Mesa) needs 1.4 or later" >&2
        echo "pip install --user 'meson>=1.4' (or pipx install meson), and put it first in PATH" >&2
        return 1
    fi
}

step_toolchain() {
    check_tools || return 1
    if [[ ! -d $tri/.git ]]; then
        mkdir -p "$(dirname "$tri")"
        git clone "$tri_url" "$tri" || return 1
    fi
    git -C "$tri" cat-file -e "$tri_rev^{commit}" 2> /dev/null || git -C "$tri" fetch origin "$tri_rev" || return 1
    git -C "$tri" checkout -q --detach "$tri_rev" || return 1
    echo "ps5-plume-triangle at $(git -C "$tri" rev-parse HEAD)"
    (cd "$tri" && bash tools/build.sh) || return 1
    local f
    for f in .deps/native/ps5-payload-sdk/bin/prospero-lld \
             .deps/native/radv-release/lib/libvulkan_radeon.ps5.a build/host/ps5-native-tool \
             build/obj/app_crt.o build/obj/app_cpp_runtime.o build/stubs/libSceAgc.so runtime/libc.prx; do
        [[ -e $tri/$f ]] || { echo "the toolchain build left no $f" >&2; return 1; }
    done
}

step_sdk() {
    "$repo/ps5/tools/fetch_sdk.sh"
}

step_compile() {
    local sdk_root=$tri/.deps/native/ps5-payload-sdk
    [[ -x $sdk_root/bin/prospero-lld ]] || { echo "no PS5 payload SDK in $tri: run the toolchain step" >&2; return 1; }
    cmake -S "$repo/ps5" -B "$build" -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_TOOLCHAIN_FILE="$repo/ps5/cmake/ps5-toolchain.cmake" \
        -DPS5_PAYLOAD_SDK="$sdk_root" -DCMAKE_POLICY_VERSION_MINIMUM=3.5 || return 1
    ninja -C "$build" ${DS2_PS5_JOBS:+-j"$DS2_PS5_JOBS"} ds2
}

step_package() {
    "$repo/ps5/tools/ps5_package.sh" "$build" "$tri"
}

step_zip() {
    local apps=("$build"/ps5-app/*/)
    [[ -f ${apps[0]}eboot.bin ]] || { echo "no packaged app in $build/ps5-app: run the package step" >&2; return 1; }
    local out=$build/DeadSpace2-PS5-$version
    rm -f -- "$out.zip"
    python3 -c 'import shutil, sys; shutil.make_archive(sys.argv[1], "zip", sys.argv[2], sys.argv[3])' \
        "$out" "$build/ps5-app" "$(basename "${apps[0]}")" || return 1
    echo "$out.zip ($(du -h "$out.zip" | cut -f1))"
}

mkdir -p "$logs"
for s in "${steps[@]}"; do
    start=$SECONDS
    echo "==> [$s] ... ($logs/$s.log)"
    if "step_$s" > "$logs/$s.log" 2>&1; then
        echo "==> [$s] done in $(( (SECONDS - start) / 60 )) min"
        [[ $s != zip ]] || tail -1 "$logs/$s.log"
    else
        echo "==> [$s] FAILED after $(( (SECONDS - start) / 60 )) min; the end of $logs/$s.log:" >&2
        tail -40 "$logs/$s.log" >&2
        exit 1
    fi
done
