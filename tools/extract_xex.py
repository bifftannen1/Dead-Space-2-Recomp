#!/usr/bin/env python3
"""Extract default.xex from an Xbox 360 disc image (XDVDFS), read-only on the ISO.

usage: python tools/extract_xex.py <disc.iso> <out/default.xex>

Same layout rules as the SDK's DiscImageDevice (src/filesystem/devices/disc_image_device.cpp):
game partition at one of the known offsets, volume descriptor "MICROSOFT*XBOX*MEDIA" at sector 32
of the partition, root directory sector/size at +20/+24, little-endian binary-tree directory.
Prints EXTRACT_XEX_OK <sha256> <bytes> only when the file was written with the size the directory
entry declares; exits 1 otherwise.
"""
from __future__ import annotations

import hashlib
import struct
import sys
from pathlib import Path

SECTOR = 2048
OFFSETS = (0x00000000, 0x0000FB20, 0x00020600, 0x02080000, 0x0FD90000)
MAGIC = b"MICROSOFT*XBOX*MEDIA"


def find_root(f) -> tuple[int, int, int]:
    for off in OFFSETS:
        f.seek(off + 32 * SECTOR)
        hdr = f.read(28)
        if hdr[:20] == MAGIC:
            sector, size = struct.unpack_from("<II", hdr, 20)
            return off, sector, size
    raise SystemExit("EXTRACT_XEX_FAIL no XDVDFS volume descriptor at any known partition offset")


def find_entry(f, game_off: int, sector: int, size: int, want: str) -> tuple[int, int]:
    f.seek(game_off + sector * SECTOR)
    table = f.read(size)
    seen = set()
    stack = [0]
    while stack:
        node = stack.pop()
        pos = node * 4
        if pos in seen or pos + 14 > len(table):
            continue
        seen.add(pos)
        left, right, esec, elen, _attr, nlen = struct.unpack_from("<HHIIBB", table, pos)
        if left == 0xFFFF and right == 0xFFFF:
            continue
        name = table[pos + 14:pos + 14 + nlen].decode("ascii", "replace")
        if name.lower() == want:
            return esec, elen
        for child in (left, right):
            if child not in (0, 0xFFFF):
                stack.append(child)
    raise SystemExit(f"EXTRACT_XEX_FAIL {want} not in the root directory")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    iso, out = Path(sys.argv[1]), Path(sys.argv[2])
    with iso.open("rb") as f:
        game_off, rsec, rsize = find_root(f)
        esec, elen = find_entry(f, game_off, rsec, rsize, "default.xex")
        f.seek(game_off + esec * SECTOR)
        data = f.read(elen)
    if len(data) != elen or not data.startswith(b"XEX2"):
        print("EXTRACT_XEX_FAIL short read or not an XEX2 image")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    if out.stat().st_size != elen:
        print("EXTRACT_XEX_FAIL write size mismatch")
        return 1
    print("EXTRACT_XEX_OK", hashlib.sha256(data).hexdigest(), elen, f"partition=0x{game_off:X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
