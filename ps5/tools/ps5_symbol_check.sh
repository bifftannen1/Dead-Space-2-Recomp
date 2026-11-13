#!/usr/bin/env bash
# Dead Space 2 for PS5: a link-level check without the PS5 toolchain's linker.
#
#   PS5_CHECK_OBJ_DIR=<objects> ps5/tools/ps5_syntax_check.sh <ps5 headers>
#   ps5/tools/ps5_symbol_check.sh <objects> [PS5_PayloadSDK source tree]
#
# From the object files of the SDK and the app it reports:
#   - symbols defined more than once (a duplicate-symbol error at link time);
#   - SDK and app symbols that are used but defined nowhere;
#   - with the payload SDK's source tree, C symbols that none of its libraries
#     export (sce_stubs/*.c) - a function the console doesn't have.
# What is left for the libraries the build links (the C++ library, SDL, FFmpeg,
# glslang, ImGui, ...) and for the recompiled game is listed by count only.
set -uo pipefail

objs=$(realpath "${1:?directory of object files (PS5_CHECK_OBJ_DIR of ps5_syntax_check.sh)}")
sdk_src=${2:-}
nm=${NM:-$(command -v llvm-nm-19 || command -v llvm-nm-18 || command -v llvm-nm || command -v nm)}
filt=${CXXFILT:-$(command -v llvm-cxxfilt-19 || command -v llvm-cxxfilt-18 || command -v llvm-cxxfilt || command -v c++filt)}
ls "$objs"/*.o > /dev/null 2>&1 || { echo "no object files in $objs" >&2; exit 2; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
bad=0

"$nm" --defined-only -g "$objs"/*.o 2>/dev/null | awk 'NF==3 {print $3}' | sort -u > "$work/defined"
"$nm" --undefined-only -g "$objs"/*.o 2>/dev/null | awk 'NF==2 && $1=="U" {print $2}' | sort -u > "$work/undefined"
comm -23 "$work/undefined" "$work/defined" > "$work/unresolved"

# Strong definitions (not weak, not common) of one name in two objects.
"$nm" --defined-only -g "$objs"/*.o 2>/dev/null | awk 'NF==3 && $2 ~ /^[TDBRS]$/ {print $3}' |
    sort | uniq -d > "$work/dup"
if [[ -s $work/dup ]]; then
    echo "==> defined more than once:"
    "$filt" < "$work/dup" | sed 's/^/    /'
    bad=1
fi

# The SDK's and the app's own names: everything used has to be defined here.
grep -E '^_ZN?K?(3rex|6ds2ps5|3ds2)|^rex_' "$work/unresolved" > "$work/own"
if [[ -s $work/own ]]; then
    echo "==> SDK or app symbols used but not defined:"
    "$filt" < "$work/own" | sed 's/^/    /'
    bad=1
fi

# C symbols: the console's libraries, the payload SDK's libc and platform
# library, the build's C libraries, the recompiled game, the linker, and
# ps5_emutls.c (ps5_package.sh links it).
grep -v '^_Z' "$work/unresolved" |
    grep -vE '^(SDL_|av_|avcodec_|avformat_|avutil_|sws_|swr_|aes_|XXH|ZSTD|mspack|lzx|o1heap|__imp__|sub_|PPCImageConfig$|__dso_handle$|__emutls_get_address$|ps5_(shm|vrange)_)' \
    > "$work/c"
if [[ -n $sdk_src ]]; then
    [[ -d $sdk_src/sce_stubs ]] || { echo "$sdk_src has no sce_stubs" >&2; exit 2; }
    {
        grep -ohE '\.global [A-Za-z0-9_]+' "$sdk_src"/sce_stubs/*.c | awk '{print $2}'
        ls "$sdk_src/libc" | sed 's/\..*//'
    } | sort -u > "$work/console"
    comm -23 "$work/c" "$work/console" > "$work/missing"
    if [[ -s $work/missing ]]; then
        echo "==> C symbols the payload SDK's libraries don't list:"
        sed 's/^/    /' "$work/missing"
        bad=1
    fi
    echo "==> $(wc -l < "$work/c") C library symbols, $(comm -12 "$work/c" "$work/console" | wc -l) of them in the payload SDK's lists"
else
    echo "==> $(wc -l < "$work/c") C library symbols (not checked: no payload SDK source tree given)"
fi

echo "==> $(wc -l < "$work/defined") symbols defined, $(wc -l < "$work/unresolved") left for the libraries and the recompiled game"
(( bad == 0 )) && echo "==> no duplicate or missing symbols"
exit $bad
