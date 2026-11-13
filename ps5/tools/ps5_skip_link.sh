#!/usr/bin/env bash
# CMake's own link of the PS5 program would not produce anything that runs:
# the real link is ps5_package.sh's, with the PS5 Vulkan driver, the app start-up
# code and the PS5 linker script. This stands in for CMake's link and only
# writes the output's name (and, with ThinLTO, saves generating the whole
# program's code twice).
out=
while [[ $# -gt 0 ]]; do
    if [[ $1 == -o ]]; then out=$2; shift; fi
    shift
done
[[ -n $out ]] && : > "$out"
exit 0
