#!/usr/bin/env python3
r"""disc_files.py -- list or extract files of the Xbox 360 disc image (read-only on the ISO).

usage: python tools/modapi/disc_files.py <disc.iso> list [--match REGEX]
       python tools/modapi/disc_files.py <disc.iso> extract <guest path> <out file>

Same XDVDFS layout rules as tools/extract_xex.py (and the SDK's DiscImageDevice). Paths are
the game's: backslash-separated, relative to game:\ (e.g. `movies\en\trio_frontend.vp6`),
matched case-insensitively. `list` prints `<size>\t<path>` and LIST_OK <files>; `extract`
prints EXTRACT_OK <bytes> <fnv1a64> only when the size the directory declares was written.
Used to build loose-file mods from the user's own disc (the result stays out of git).
"""
from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from extract_xex import SECTOR, find_root  # noqa: E402


def walk(f, game_off: int, sector: int, size: int, prefix: str = ""):
    """Yield (path, start_sector, size, is_dir) for the binary-tree directory at sector."""
    if size == 0:
        return
    f.seek(game_off + sector * SECTOR)
    data = f.read(size)
    seen = set()
    stack = [0]
    while stack:
        off = stack.pop()
        if off in seen or off + 14 > len(data):
            continue
        seen.add(off)
        left, right, start, length, attr, nlen = struct.unpack_from("<HHIIBB", data, off)
        if left == 0xFFFF and right == 0xFFFF:
            continue
        name = data[off + 14: off + 14 + nlen].decode("ascii", "replace")
        if left:
            stack.append(left * 4)
        if right:
            stack.append(right * 4)
        path = prefix + name
        is_dir = bool(attr & 0x10)
        yield path, start, length, is_dir
        if is_dir:
            yield from walk(f, game_off, start, length, path + "\\")


def fnv1a64(b: bytes) -> int:
    h = 1469598103934665603
    for x in b:
        h = ((h ^ x) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    iso, cmd = Path(sys.argv[1]), sys.argv[2]
    with open(iso, "rb") as f:
        game_off, sector, size = find_root(f)
        entries = list(walk(f, game_off, sector, size))
        if cmd == "list":
            rx = re.compile(sys.argv[4], re.I) if len(sys.argv) > 4 and sys.argv[3] == "--match" else None
            n = 0
            for p, _, ln, d in sorted(entries):
                if d or (rx and not rx.search(p)):
                    continue
                print(f"{ln}\t{p}")
                n += 1
            print("LIST_OK", n)
            return 0 if n else 1
        if cmd == "extract":
            want, out = sys.argv[3].lower(), Path(sys.argv[4])
            for p, start, ln, d in entries:
                if not d and p.lower() == want:
                    f.seek(game_off + start * SECTOR)
                    b = f.read(ln)
                    if len(b) != ln:
                        print("EXTRACT_FAIL short read")
                        return 1
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(b)
                    print("EXTRACT_OK", ln, f"{fnv1a64(b):016x}")
                    return 0
            print("EXTRACT_FAIL not on the disc:", sys.argv[3])
            return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
