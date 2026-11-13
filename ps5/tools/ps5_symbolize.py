#!/usr/bin/env python3
"""Turn the addresses in a Dead Space 2 for PS5 log into function names.

usage: python3 ps5/tools/ps5_symbolize.py <out/ps5/ps5-link/build/llvm-pie.elf> <log file or ->

The app's "[ps5 crash]" line and its "ps5 stack:" lines print code addresses
relative to an anchor inside the program plus 0x10000000 (the program is loaded
at a different address on every start). This looks the anchor up in the linked
program (kept by ps5_package.sh) and prints each such line again with
function names and, for the recompiled game code, source lines.

Needs llvm-nm and llvm-symbolizer (any LLVM; versioned names are found too).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys

ANCHOR = "rex::ps5diag::NowMs()"
BIAS = 0x10000000


def tool(name: str) -> str:
    for candidate in [name] + [f"{name}-{v}" for v in range(21, 13, -1)]:
        if shutil.which(candidate):
            return candidate
    sys.exit(f"{name} not found")


def anchor_address(elf: str) -> int:
    out = subprocess.run([tool("llvm-nm"), "-C", "--defined-only", elf], capture_output=True, text=True, check=True)
    for line in out.stdout.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[2].strip() == ANCHOR:
            return int(parts[0], 16)
    sys.exit(f"{ANCHOR} is not in {elf}: is it the llvm-pie.elf of the build that wrote the log?")


def relative_addresses(line: str) -> list[int]:
    """The program-relative code addresses in a crash or stack line, in the order printed."""
    if "[ps5 crash]" in line:
        # "... (rip <absolute>, anchor-relative <relative>) | stack: <relative> <relative> ..."
        head, _, stack = line[line.index("[ps5 crash]"):].partition("| stack:")
        found = re.findall(r"anchor-relative ([0-9A-F]+)", head) + re.findall(r"\b[0-9A-F]+\b", stack)
    else:
        # "ps5 stack: tid <n> <name>: rip <relative> | <relative> <relative> ..."
        head, _, stack = line[line.index("ps5 stack:"):].partition("|")
        found = re.findall(r"rip ([0-9A-F]+)", head) + re.findall(r"\b[0-9A-F]+\b", stack)
    values = [int(v, 16) for v in found]
    # (the program is under 256 MB either side of the anchor)
    return [v for v in values if 0 < v < 2 * BIAS]


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    elf, log = sys.argv[1], sys.argv[2]
    anchor = anchor_address(elf)
    text = sys.stdin.read() if log == "-" else open(log, encoding="utf-8", errors="replace").read()
    symbolizer = tool("llvm-symbolizer")
    for line in text.splitlines():
        if "[ps5 crash]" not in line and "ps5 stack:" not in line:
            continue
        print(line)
        if "ps5 stack: anchor" in line:
            continue
        values = relative_addresses(line)
        if not values:
            continue
        addresses = [hex(v - BIAS + anchor) for v in values]
        out = subprocess.run([symbolizer, "-e", elf, "-C", "-p", *addresses], capture_output=True, text=True)
        for value, name in zip(values, out.stdout.strip().splitlines()):
            print(f"    {value:X}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
