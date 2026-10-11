#!/usr/bin/env python3
r"""extract_dlc.py -- install an Xbox 360 DLC package (STFS: LIVE/PIRS/CON) for ds2.exe.

    python tools/extract_dlc.py <package file> --user-root <user data root>     # install
    python tools/extract_dlc.py <package file> --list                           # list only

The runtime mounts DLC as a plain folder (ReXGlue ContentManager, Xenia layout):
    <user root>/0000000000000000/<title id>/<content type>/<package file name>/
so this reads the package's own file table and writes every file there. Title id and content type
come from the package header. Run ds2.exe with --user_data_root=<user root>.

Read-only packages only (the volume descriptor's read-only flag; every retail DLC tested): a writable
package (two hash tables per level) is refused, not guessed. A file whose blocks are not contiguous is
followed through the level-0 hash table's next-block field. The written size of every file must equal
its directory size, or the run fails. Nothing is written with --list.
"""
from __future__ import annotations

import argparse, os, struct, sys
from pathlib import Path

BLOCK = 0x1000


class Package:
    def __init__(self, path: Path):
        self.f = open(path, "rb")
        h = self.f.read(0xA000)
        if h[:4] not in (b"LIVE", b"PIRS", b"CON "):
            raise SystemExit(f"EXTRACT_DLC_FAIL not an STFS package (magic {h[:4]!r})")
        self.magic = h[:4].decode().strip()
        hdr_size = struct.unpack(">I", h[0x340:0x344])[0]
        self.content_type = struct.unpack(">I", h[0x344:0x348])[0]
        self.title_id = struct.unpack(">I", h[0x360:0x364])[0]
        self.display = h[0x411:0x411 + 0x100].decode("utf-16-be", "replace").split("\0")[0]
        self.base = (hdr_size + 0xFFF) & ~0xFFF
        vd = h[0x379:0x379 + 0x24]
        if not vd[2] & 1:
            raise SystemExit("EXTRACT_DLC_FAIL writable (non read-only) STFS volume: not supported")
        self.ft_count = struct.unpack("<H", vd[3:5])[0]
        self.ft_block = int.from_bytes(vd[5:8], "little")

    @staticmethod
    def backing(b: int) -> int:
        # data block -> backing block: one hash table before each 0xAA / 0x70E4 / 0x4AF768 group
        r = b
        for lvl in (0xAA, 0x70E4, 0x4AF768):
            r += (b + lvl) // lvl
            if b < lvl:
                break
        return r

    def read_block(self, b: int) -> bytes:
        self.f.seek(self.base + (self.backing(b) << 12))
        return self.f.read(BLOCK)

    def next_block(self, b: int) -> int:
        tbl = self.backing((b // 0xAA) * 0xAA) - 1     # level-0 table right before its group
        self.f.seek(self.base + (tbl << 12) + (b % 0xAA) * 0x18)
        return int.from_bytes(self.f.read(0x18)[0x15:0x18], "big")

    def blocks(self, start: int, n: int, contiguous: bool):
        b = start
        for _ in range(n):
            yield self.read_block(b)
            b = b + 1 if contiguous else self.next_block(b)

    def entries(self):
        ft = b"".join(self.blocks(self.ft_block, self.ft_count, self.ft_count == 1))
        out = []
        for i in range(0, len(ft), 0x40):
            e = ft[i:i + 0x40]
            n = e[0x28] & 0x3F
            if n == 0:
                out.append(None)
                continue
            out.append(dict(name=e[:n].decode("ascii", "replace"), dir=bool(e[0x28] & 0x80),
                            contig=bool(e[0x28] & 0x40), blocks=int.from_bytes(e[0x29:0x2C], "little"),
                            start=int.from_bytes(e[0x2F:0x32], "little"),
                            parent=struct.unpack(">h", e[0x32:0x34])[0], size=struct.unpack(">I", e[0x34:0x38])[0]))
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("package")
    ap.add_argument("--user-root", help="user data root (ds2.exe --user_data_root)")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    pkg_path = Path(a.package)
    p = Package(pkg_path)
    ents = p.entries()

    def path_of(i):
        e = ents[i]
        return e["name"] if e["parent"] == -1 else path_of(e["parent"]) + "/" + e["name"]
    files = [(i, e) for i, e in enumerate(ents) if e and not e["dir"]]
    print(f"{p.magic} '{p.display}' title {p.title_id:08X} type {p.content_type:08X}: "
          f"{len(files)} files, {sum(e['size'] for _, e in files)} bytes")
    if a.list or not a.user_root:
        for i, e in files:
            print(f"{e['size']:>12}  {path_of(i)}")
        return 0
    dest = Path(a.user_root) / "0000000000000000" / f"{p.title_id:08X}" / f"{p.content_type:08X}" / pkg_path.name
    for i, e in enumerate(ents):
        if e and e["dir"]:
            (dest / path_of(i)).mkdir(parents=True, exist_ok=True)
    for i, e in files:
        out = dest / path_of(i)
        out.parent.mkdir(parents=True, exist_ok=True)
        left = e["size"]
        with open(out, "wb") as o:
            for blk in p.blocks(e["start"], e["blocks"], e["contig"]):
                o.write(blk[:min(BLOCK, left)])
                left -= BLOCK
                if left <= 0:
                    break
        if out.stat().st_size != e["size"]:
            print(f"EXTRACT_DLC_FAIL {path_of(i)}: wrote {out.stat().st_size} of {e['size']} bytes")
            return 1
    print(f"EXTRACT_DLC_OK {len(files)} files -> {dest}")
    print(f"run: ds2.exe --user_data_root={a.user_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
