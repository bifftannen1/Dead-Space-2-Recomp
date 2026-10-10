"""PE32 image reader (mapped or raw layout) with Xenon .pdata decoding.

Semantics follow xb360_toolkit/lanes/identity_image.py (the reader every DS2 measurement used):
a read that crosses a section end, falls in an unmapped gap or needs missing file bytes fails
(reader() turns that into None), never zero-fills silently. For an XEX, extract the basefile PE
first (the PE is what the PDB's RSDS identity and section table describe).
"""
from __future__ import annotations

import bisect, hashlib, struct
from pathlib import Path


class ImageError(ValueError):
    pass


class PEImage:
    def __init__(self, path, layout="mapped"):
        self.path = Path(path).resolve()
        self.data = self.path.read_bytes()
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        d = self.data
        if len(d) < 64 or d[:2] != b"MZ":
            raise ImageError("invalid DOS header")
        pe = struct.unpack_from("<I", d, 60)[0]
        if d[pe:pe + 4] != b"PE\0\0":
            raise ImageError("invalid PE signature")
        self.machine, nsec, self.timestamp = struct.unpack_from("<HHI", d, pe + 4)
        optsize = struct.unpack_from("<H", d, pe + 20)[0]
        opt = pe + 24
        if struct.unpack_from("<H", d, opt)[0] != 0x10B:
            raise ImageError("only PE32 is supported")
        self.base = struct.unpack_from("<I", d, opt + 28)[0]
        self.size_of_image, self.size_of_headers = struct.unpack_from("<II", d, opt + 56)
        ndir = struct.unpack_from("<I", d, opt + 92)[0]
        self.directories = [dict(zip(("rva", "size"), struct.unpack_from("<II", d, opt + 96 + 8 * i))) | {"index": i}
                            for i in range(ndir)]
        self.sections = []
        for i in range(nsec):
            q = opt + optsize + 40 * i
            name, vsize, rva, rsize, raw = struct.unpack_from("<8sIIII", d, q)
            flags = struct.unpack_from("<I", d, q + 36)[0]
            self.sections.append({"index": i + 1, "name": name.rstrip(b"\0").decode("ascii", "replace"),
                                  "rva": rva, "va": self.base + rva, "raw": raw, "rsize": rsize, "vsize": vsize,
                                  "mapped_size": max(vsize, rsize), "characteristics": flags,
                                  "executable": bool(flags & 0x20000000)})
        if layout not in ("mapped", "raw"):
            raise ImageError("layout must be mapped or raw")
        self.layout = layout
        for s in self.sections:
            off = s["rva"] if layout == "mapped" else s["raw"]
            req = s["mapped_size"] if layout == "mapped" else s["rsize"]
            avail = min(req, max(0, len(d) - off))
            s["available_size"] = s["mapped_size"] if layout == "raw" and avail == req else avail
        self._ordered = sorted(self.sections, key=lambda s: s["va"])
        self._starts = [s["va"] for s in self._ordered]
        self._pdata = None

    def section_for_va(self, va):
        i = bisect.bisect_right(self._starts, va) - 1
        if i >= 0:
            s = self._ordered[i]
            if va < s["va"] + s["mapped_size"]:
                return s
        return None

    def _slice(self, off, n):
        if off < 0 or n < 0 or off + n > len(self.data):
            raise ImageError("truncated read")
        return self.data[off:off + n]

    def read_va(self, va, size):
        if not isinstance(size, int) or size < 0 or va < self.base or va + size > 0x100000000:
            raise ImageError("invalid virtual read")
        rva = va - self.base
        if rva < self.size_of_headers:
            if rva + size > self.size_of_headers:
                raise ImageError("read crosses headers")
            return self._slice(rva, size)
        s = self.section_for_va(va)
        if s is None or va + size > s["va"] + s["mapped_size"]:
            raise ImageError("unmapped or cross-section read")
        delta = va - s["va"]
        if delta + size > s["available_size"]:
            raise ImageError("unavailable bytes")
        if self.layout == "mapped":
            return self._slice(rva, size)
        init = max(0, min(size, s["rsize"] - delta))
        raw = self._slice(s["raw"] + delta, init) if init else b""
        return raw + bytes(size - init)

    def pdata(self):
        """Xenon packed .pdata: (begin, length, prolog) per function; 8-byte records."""
        if self._pdata is not None:
            return list(self._pdata)
        if self.machine != 0x1F2:
            raise ImageError("not a big-endian PowerPC (Xenon) image")
        rows = []
        for s in self.sections:
            if s["name"] != ".pdata":
                continue
            size = s["vsize"] or s["rsize"]
            raw = self.read_va(s["va"], size)
            for off in range(0, size - size % 8, 8):
                va, packed = struct.unpack_from(">II", raw, off)
                if not va and not packed:
                    continue
                rows.append({"va": va, "size": ((packed >> 8) & 0x3FFFFF) * 4, "prolog": (packed & 255) * 4})
        self._pdata = rows
        return list(rows)


def reader(im):
    """va, n -> bytes or None (a failed read is None, never zeros)."""
    def rd(va, n):
        try:
            return im.read_va(va, n)
        except Exception:
            return None
    return rd
