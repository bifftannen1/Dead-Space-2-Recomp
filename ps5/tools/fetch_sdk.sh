#!/usr/bin/env bash
# Dead Space 2 for PS5: the ReXGlue SDK source the PS5 build compiles.
#
#   ps5/tools/fetch_sdk.sh [directory]
#
# Clones rexglue-sdk at the commit pinned in sdk/REXGLUE_BASE.txt (the same one
# the PC build uses) with its third-party submodules, then applies, in order:
#   sdk/rexglue_ds2.patch   the Dead Space 2 fixes (sha256 pinned, as on the PC)
#   sdk/rexglue_ps5.patch   the PS5 platform layer, on top of it
#   sdk/ffmpeg_ps5.patch    FFmpeg's configuration for the PS5 (in thirdparty/FFmpeg)
# Safe to run again: what is already applied is left alone.
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
sdk=${1:-$repo/thirdparty/rexglue-sdk-ps5}

pin() { sed -n "s/^$1 *= *//p" "$repo/sdk/REXGLUE_BASE.txt" | head -1 | awk '{print $1}'; }
url=$(pin upstream_repo)
commit=$(pin upstream_commit)
patch_sha=$(pin patch_sha256)
[[ -n $url && -n $commit && -n $patch_sha ]] || { echo "sdk/REXGLUE_BASE.txt has no pin" >&2; exit 2; }

got=$(sha256sum "$repo/sdk/rexglue_ds2.patch" | awk '{print $1}')
[[ $got == "$patch_sha" ]] || { echo "sdk/rexglue_ds2.patch sha256 $got != pinned $patch_sha" >&2; exit 2; }

if [[ ! -d $sdk/.git ]]; then
    mkdir -p "$sdk"
    git -C "$sdk" init -q
    git -C "$sdk" remote add origin "$url"
    echo "==> [sdk] fetching rexglue-sdk $commit"
    git -C "$sdk" fetch -q --depth 1 origin "$commit"
    git -C "$sdk" -c advice.detachedHead=false checkout -q FETCH_HEAD
fi
head=$(git -C "$sdk" rev-parse HEAD)
[[ $head == "$commit" ]] || { echo "$sdk is at $head, not the pinned $commit" >&2; exit 2; }
echo "==> [sdk] third-party submodules"
git -C "$sdk" submodule update --init --recursive --depth 1 --jobs 8

# apply_once <git dir> <patch> <name>
apply_once() {
    local dir=$1 patch=$2 name=$3
    if git -C "$dir" apply --check --reverse --whitespace=nowarn "$patch" 2>/dev/null; then
        echo "==> [sdk] $name: already applied"
    elif git -C "$dir" apply --check --whitespace=nowarn "$patch" 2>/dev/null; then
        git -C "$dir" apply --whitespace=nowarn "$patch"
        echo "==> [sdk] $name: applied"
    else
        echo "$name neither applies nor is already applied in $dir" >&2
        exit 2
    fi
}
# The PS5 patch is made against the tree with the Dead Space 2 patch in it, and
# touches some of the same files: once it is applied the first can no longer be
# checked on its own, so the PS5 patch is looked at first.
if git -C "$sdk" apply --check --reverse --whitespace=nowarn "$repo/sdk/rexglue_ps5.patch" 2>/dev/null; then
    echo "==> [sdk] rexglue_ds2.patch + rexglue_ps5.patch: already applied"
else
    apply_once "$sdk" "$repo/sdk/rexglue_ds2.patch" rexglue_ds2.patch
    apply_once "$sdk" "$repo/sdk/rexglue_ps5.patch" rexglue_ps5.patch
fi
apply_once "$sdk/thirdparty/FFmpeg" "$repo/sdk/ffmpeg_ps5.patch" ffmpeg_ps5.patch
echo "==> [sdk] ready: $sdk"
