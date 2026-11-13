#!/usr/bin/env bash
# Dead Space 2 for PS5: link the PS5 build and package it as a PS5 homebrew app
# folder (eboot.bin, sce_module/libc.prx, sce_sys).
#
#   ps5/tools/ps5_package.sh <cmake build dir> <ps5-plume-triangle checkout> [title id]
#
# After the GTA IV PS5 port's ps5_package.sh. The link and packaging reuse the
# PS5 tooling of Nazky's ps5-plume-triangle (mihawk-99's RADV link recipe, app
# CRT, libc.prx and FSELF signer): build that project once first (its build/
# holds app_crt.o, app_cpp_runtime.o, the libSceAgc stubs and ps5-native-tool).
# No game files are packaged: they go in the app's install folder on the PS5.
set -euo pipefail

build=$(realpath "${1:?cmake build dir}")
tri=$(realpath "${2:?ps5-plume-triangle checkout}")
title_id=${3:-PPSA94108}
ps5dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
version=$(head -1 "$ps5dir/VERSION.txt")

sdk_root=${PS5_PAYLOAD_SDK:-$tri/.deps/native/ps5-payload-sdk}
radv_archive=${RADV_ARCHIVE:-$tri/.deps/native/radv-release/lib/libvulkan_radeon.ps5.a}
tool=$tri/build/host/ps5-native-tool
for f in "$sdk_root/bin/prospero-lld" "$radv_archive" "$tool" "$tri/build/obj/app_crt.o" \
         "$tri/build/obj/app_cpp_runtime.o" "$tri/build/stubs/libSceAgc.so" "$tri/runtime/libc.prx"; do
    [[ -e $f ]] || { echo "missing $f (build ps5-plume-triangle first: ps5/BUILD.md)" >&2; exit 2; }
done

# Staging root for radv_link_recipe: it reads tooling/ and vendor/ and writes build/.
stage=$build/ps5-link
mkdir -p "$stage/build"
ln -sfn "$tri/tooling" "$stage/tooling"
ln -sfn "$tri/vendor" "$stage/vendor"

# The objects and static libraries CMake links into ds2, in its order (CMake's
# own link is skipped: ps5_skip_link.sh). A response file, if ninja used one,
# is read too.
link_cmd=$(ninja -C "$build" -t commands ds2 | tail -1)
words=()
for w in $link_cmd; do
    if [[ $w == @* && -f $build/${w#@} ]]; then
        # shellcheck disable=SC2207
        words+=($(cat "$build/${w#@}"))
    else
        words+=("$w")
    fi
done
# The runtime and the GPU backend whole. On the PC they are DLLs, loaded
# entire: every kernel export and every setting registers itself from a static
# object. Out of a static archive the linker takes only the members something
# refers to, and an export nothing calls by name would go missing without a
# word. PS5_WHOLE_ARCHIVE=0 links them as plain archives.
whole=${PS5_WHOLE_ARCHIVE:-1}
inputs=()
declare -A seen=()
for w in "${words[@]}"; do
    case $w in
        */libz.a) continue ;;  # (RADV's archive has zlib in it)
        *.o|*.a) [[ $w = /* ]] || w=$build/$w ;;
        *) continue ;;
    esac
    # CMake names a static library again after the ones that depend on it. The
    # link below is one group, so once is enough, and a library taken whole
    # twice would define everything in it twice.
    [[ -z ${seen[$w]:-} ]] || continue
    seen[$w]=1
    case $w in
        */librexruntime.a|*/librexgpu-xenos.a)
            if [[ $whole == 1 ]]; then inputs+=(--whole-archive "$w" --no-whole-archive); else inputs+=("$w"); fi ;;
        *) inputs+=("$w") ;;
    esac
done
count=0
for w in "${inputs[@]}"; do [[ $w == --* ]] || count=$((count + 1)); done
echo "==> [link] $count objects and libraries from CMake"
[[ $count -gt 100 ]] || { echo "that is too few: was the build (ninja -C $build ds2) run?" >&2; exit 2; }

export PS5_CLANG=${PS5_CLANG:-$(command -v clang-19 || command -v clang-18 || command -v clang)}
# shellcheck source=/dev/null
source "$tri/tools/radv-link.sh"
radv_link_recipe "$stage" "$sdk_root" "$radv_archive"
# The recipe's emulated TLS with a thread-exit fix (ps5_emutls.c), compiled
# over the recipe's object, which the link uses.
"$PS5_CLANG" --target=x86_64-sie-ps5 -fPIC -fno-plt -fno-stack-protector \
    -isysroot "$sdk_root" -isystem "$sdk_root/target/include" \
    -O2 -c "$ps5dir/tools/ps5_emutls.c" -o "$stage/build/ps5-emutls-cxa.o"

# stat("") / lstat("") as on Linux (ps5_stat.c, through --wrap).
"$sdk_root/bin/prospero-clang" -c -O2 -fno-visibility-from-dllstorageclass -o "$stage/build/ps5_stat.o" "$ps5dir/tools/ps5_stat.c"

# With ThinLTO (-DDS2_PS5_THINLTO=ON) the link finishes the build, two jobs at
# a time, with a cache so a change to one file redoes only what it touches.
lto_flags=()
if grep -q '^DS2_PS5_THINLTO:BOOL=ON' "$build/CMakeCache.txt" 2>/dev/null; then
    lto_flags=(--thinlto-jobs="${PS5_LTO_JOBS:-2}" --thinlto-cache-dir="$stage/thinlto-cache" --lto-O2)
fi
link_pie() {  # extra objects, if any
    "$sdk_root/bin/prospero-lld" "${radv_linker_script[@]}" --eh-frame-hdr "${radv_link_flags[@]}" \
        "${lto_flags[@]}" \
        --version-script "$tri/tooling/native/app-symbols.map" --exclude-libs=ALL \
        --wrap=stat --wrap=lstat \
        -e _start -o "$stage/build/llvm-pie.elf" \
        "$tri/build/obj/app_crt.o" "$tri/build/obj/app_cpp_runtime.o" "$stage/build/ps5_stat.o" "$@" \
        --start-group "${inputs[@]}" --end-group \
        "$tri/build/stubs/libSceAgc.so" "$tri/build/stubs/libSceAgcDriver.so" \
        "${radv_link_inputs[@]}" \
        --as-needed "$sdk_root"/target/lib/*.so
}
make_eboot() {
    "$tool" link --in "$stage/build/llvm-pie.elf" --out "$stage/build/eboot.elf" \
        --stub-dir "$sdk_root/target/lib" --stub "$tri/build/stubs/libSceAgc.so" \
        --stub "$tri/build/stubs/libSceAgcDriver.so" --module-sdk 0x02000009 \
        --companion-sdk 0x08050001 --file-name eboot.elf
}

echo "==> [link] Dead Space 2 + RADV into a PS5 PIE ELF"
link_pie

echo "==> [package] eboot.bin for $title_id"
if ! make_eboot 2> "$stage/build/eboot.err"; then
    cat "$stage/build/eboot.err" >&2
    # The tool appends the process parameters right after the image's RELRO
    # content, inside its last page; when that content happens to end near
    # the page boundary there is no room. Link again with half a page of
    # RELRO padding (ps5_relro_pad.c), which moves the end well clear of it.
    grep -q "no room for PS5 process parameters" "$stage/build/eboot.err" || exit 1
    echo "==> [link] again with 2 KB of RELRO padding (room for the process parameters)"
    "$sdk_root/bin/prospero-clang" -c -O2 -fPIC -fno-visibility-from-dllstorageclass \
        -o "$stage/build/ps5_relro_pad.o" "$ps5dir/tools/ps5_relro_pad.c"
    link_pie "$stage/build/ps5_relro_pad.o"
    make_eboot
fi
app=$build/ps5-app/$title_id
rm -rf -- "$app"
mkdir -p "$app/sce_sys" "$app/sce_module" "$app/install"
"$tool" self --sign --in "$stage/build/eboot.elf" --out "$app/eboot.bin" --magic 0x1D3D154F
cp "$tri/runtime/libc.prx" "$app/sce_module/libc.prx"
# contentId: UP9000-<title id>_00-<16 characters>
cat > "$app/sce_sys/param.json" <<JSON
{
  "ageLevel": { "default": 0 },
  "applicationCategoryType": 0,
  "applicationDrmType": "free",
  "attribute": 0,
  "attribute2": 0,
  "attribute3": 0,
  "conceptId": "${title_id:4}",
  "contentBadgeType": 1,
  "contentId": "UP9000-${title_id}_00-DEADSPACE2PS5000",
  "contentVersion": "01.000.000",
  "downloadDataSize": 256,
  "gameIntent": { "permittedIntents": [ { "intentType": "launchActivity" } ] },
  "localizedParameters": {
    "defaultLanguage": "en-US",
    "en-US": { "titleName": "Dead Space 2" }
  },
  "masterVersion": "01.00",
  "pubtools": { "creationDate": "$(date -u +%Y-%m-%d) 00:00:00", "loudnessSnd0": "-28.00", "toolVersion": "2.00" },
  "requiredSystemSoftwareVersion": "0x0000000000000000",
  "sdkVersion": "0x0000000000000000",
  "titleId": "${title_id}",
  "versionFileUri": ""
}
JSON
# The home screen icon, where the player's files go, and the settings template.
cp -r "$ps5dir/package/sce_sys/." "$app/sce_sys/"
sed "s/PPSA94108/$title_id/g" "$ps5dir/package/install/README.txt" > "$app/install/README.txt"
cp "$ps5dir/package/ds2.example.toml" "$app/ds2.example.toml"
printf '%s\n' "$version" > "$app/VERSION.txt"
"$tool" self --inspect --file "$app/eboot.bin" | tail -3
echo "==> $app ($(stat -c %s "$app/eboot.bin") bytes), Dead Space 2 for PS5 $version"
echo "    the program with its symbols, for ps5/tools/ps5_symbolize.py: $stage/build/llvm-pie.elf"
