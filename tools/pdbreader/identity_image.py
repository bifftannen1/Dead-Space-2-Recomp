"""Strict, explicit-layout PE32 reader for the universal identity pipeline.

PE headers are little endian: Microsoft PE/COFF specification,
https://learn.microsoft.com/en-us/windows/win32/debug/pe-format .
Xenon .pdata follows the shipped XDK callstd_compressed_pdata.htm: its
high two bits are a FunctionType enum, NOT Windows CE's independent flags.
Hash-bound local SDK evidence is emitted by identity_sdk_audit.py.
Missing input bytes are NEVER silently replaced by zeros. Raw-layout virtual
zero fill is distinct from missing initialized bytes and is explicitly recorded.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import struct
from pathlib import Path


class ImageError(ValueError):
    """Malformed image, ambiguous layout, or unavailable requested bytes."""


def h32(value):
    return "0x%08X" % value


class PEImage:
    def __init__(self, path, layout="mapped"):
        self.path = Path(path).resolve()
        self.data = self.path.read_bytes()
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        self.holds = []
        self.sections = []
        self._pdata_cache = None
        if len(self.data) < 64 or self.data[:2] != b"MZ":
            raise ImageError("invalid or truncated DOS header")
        pe = self._unpack("<I", 60)[0]
        if self._slice(pe, 4) != b"PE\0\0":
            raise ImageError("invalid PE signature")
        self.machine, nsec, self.timestamp = self._unpack("<HHI", pe + 4)
        optsize = self._unpack("<H", pe + 20)[0]
        opt = pe + 24
        if not nsec or nsec > 96:
            raise ImageError("invalid section count")
        if optsize < 96 or self._unpack("<H", opt)[0] != 0x10B:
            raise ImageError("only complete PE32 optional headers are supported")
        self._slice(opt, optsize)
        self.base = self._unpack("<I", opt + 28)[0]
        self.entrypoint = self.base + self._unpack("<I", opt + 16)[0]
        self.size_of_image, self.size_of_headers = self._unpack("<II", opt + 56)
        if not self.size_of_headers or self.size_of_headers > len(self.data):
            raise ImageError("truncated or invalid SizeOfHeaders")
        if not self.size_of_image or self.base + self.size_of_image > 0x100000000:
            raise ImageError("invalid 32-bit image span")
        directory_count = self._unpack("<I", opt + 92)[0]
        if directory_count > (optsize - 96) // 8:
            raise ImageError("data directories exceed optional header")
        self.directories = []
        for index in range(directory_count):
            rva, size = self._unpack("<II", opt + 96 + index * 8)
            self.directories.append({"index": index, "rva": rva, "size": size})
        table = opt + optsize
        if table + nsec * 40 > self.size_of_headers:
            raise ImageError("section table exceeds SizeOfHeaders")
        for i in range(nsec):
            q = table + i * 40
            name, vsize, rva, rsize, raw = self._unpack("<8sIIII", q)
            flags = self._unpack("<I", q + 36)[0]
            mapped_size = max(vsize, rsize)
            if rva < self.size_of_headers and mapped_size:
                raise ImageError("section overlaps PE headers")
            if rva + mapped_size > self.size_of_image:
                raise ImageError("section exceeds SizeOfImage")
            if raw < self.size_of_headers and rsize:
                raise ImageError("raw section overlaps PE headers")
            self.sections.append({
                "index": i + 1, "name": name.rstrip(b"\0").decode("ascii", "replace"),
                "rva": rva, "va": self.base + rva, "raw": raw,
                "rsize": rsize, "vsize": vsize, "raw_size": rsize,
                "virtual_size": vsize, "mapped_size": mapped_size,
                "characteristics": flags, "executable": bool(flags & 0x20000000),
            })
        self._check_overlap("rva", "mapped_size", "virtual")
        self._check_overlap("raw", "rsize", "raw")
        self.layout_evidence = "explicit user selection"
        if layout == "auto":
            # Equivalence is geometric, not a guessed prologue hit-rate.
            if all(s["rva"] == s["raw"] and s["vsize"] <= s["rsize"]
                   for s in self.sections if s["mapped_size"]):
                layout = "mapped"
                self.layout_evidence = "raw and mapped section mappings identical"
            else:
                sidecar = Path(str(self.path) + ".layout.json")
                if not sidecar.exists():
                    raise ImageError("AMBIGUOUS_LAYOUT: select mapped/raw or provide hash-bound .layout.json")
                info = json.loads(sidecar.read_text(encoding="utf-8"))
                if (info.get("sha256", "").lower() != self.sha256 or
                        info.get("layout") not in ("mapped", "raw")):
                    raise ImageError("layout sidecar does not bind this image hash")
                layout = info["layout"]
                self.layout_evidence = "hash-bound layout sidecar: " + str(sidecar)
        if layout not in ("mapped", "raw"):
            raise ImageError("layout must be mapped, raw, or auto")
        self.layout = layout
        for s in self.sections:
            off = s["rva"] if layout == "mapped" else s["raw"]
            required = s["mapped_size"] if layout == "mapped" else s["rsize"]
            available = min(required, max(0, len(self.data) - off))
            s["file_offset"] = off
            s["initialized_available"] = available
            s["available_size"] = (s["mapped_size"] if layout == "raw" and available == required
                                   else available)
            s["virtual_zero_fill"] = max(0, s["mapped_size"] - s["rsize"]) if layout == "raw" else 0
            s["missing_bytes"] = required - available
            if s["missing_bytes"]:
                self.holds.append({"kind": "TRUNCATED_SECTION", "section": s["name"],
                                   "va": h32(s["va"] + available), "missing_bytes": s["missing_bytes"]})
        self._ordered = sorted(self.sections, key=lambda s: s["va"])
        self._starts = [s["va"] for s in self._ordered]

    def _slice(self, offset, size):
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise ImageError("truncated file read at %#x size %#x" % (offset, size))
        return self.data[offset:offset + size]

    def _unpack(self, fmt, offset):
        return struct.unpack(fmt, self._slice(offset, struct.calcsize(fmt)))

    def _check_overlap(self, start, size, label):
        rows = sorted((s for s in self.sections if s[size]), key=lambda s: s[start])
        for a, b in zip(rows, rows[1:]):
            if a[start] + a[size] > b[start]:
                raise ImageError("overlapping %s sections %s / %s" % (label, a["name"], b["name"]))

    def section_for_va(self, va):
        i = bisect.bisect_right(self._starts, va) - 1
        if i >= 0:
            s = self._ordered[i]
            if va < s["va"] + s["mapped_size"]:
                return s
        return None

    def read_va(self, va, size):
        if not isinstance(size, int) or size < 0 or va < self.base or va + size > 0x100000000:
            raise ImageError("invalid virtual read")
        rva = va - self.base
        if rva < self.size_of_headers:
            if rva + size > self.size_of_headers:
                raise ImageError("read crosses headers into unmapped gap")
            return self._slice(rva, size)
        section = self.section_for_va(va)
        if section is None or va + size > section["va"] + section["mapped_size"]:
            raise ImageError("unmapped or cross-section read at " + h32(va))
        delta = va - section["va"]
        if delta + size > section["available_size"]:
            raise ImageError("unavailable truncated section bytes at " + h32(va))
        if self.layout == "mapped":
            return self._slice(rva, size)
        initialized = max(0, min(size, section["rsize"] - delta))
        raw = self._slice(section["raw"] + delta, initialized) if initialized else b""
        return raw + bytes(size - initialized)

    def pdata(self):
        if self._pdata_cache is not None:
            return list(self._pdata_cache)
        # XDK include/xbox/winnt.h: POWERPC=0x1F0 is little endian;
        # POWERPCBE=0x1F2 is big endian. Do not reinterpret the former.
        if self.machine != 0x1F2:
            raise ImageError("unsupported machine for Xenon .pdata: %#x" % self.machine)
        rows = []
        previous_end = 0
        for section in self.sections:
            if section["name"] != ".pdata":
                continue
            # Raw alignment padding is not a declared unwind-table record.
            size = section["vsize"] or section["rsize"]
            if size % 8:
                raise ImageError(".pdata size is not an eight-byte record multiple")
            raw = self.read_va(section["va"], size)
            terminated = False
            for offset in range(0, size, 8):
                va, packed = struct.unpack_from(">II", raw, offset)
                if not va and not packed:
                    terminated = True
                    continue
                if terminated:
                    raise ImageError("nonzero .pdata record follows zero terminator")
                length = ((packed >> 8) & 0x3FFFFF) * 4
                prolog = (packed & 255) * 4
                target = self.section_for_va(va)
                if (va & 3 or not length or prolog > length or target is None or
                        not target["executable"] or va + length > target["va"] + target["mapped_size"]):
                    raise ImageError("invalid .pdata geometry at " + h32(section["va"] + offset))
                if va < previous_end:
                    raise ImageError("unsorted or overlapping .pdata function extents")
                self.read_va(va, length)
                previous_end = va + length
                function_type = packed >> 30
                rows.append({"va": va, "size": length, "len": length, "prolog": prolog,
                             "packed": packed, "function_type": function_type,
                             "function_type_name": ("save_millicode", "no_handler",
                                                    "restore_millicode", "handler")[function_type],
                             "has_exception_handler": function_type == 3,
                             "record_va": section["va"] + offset})
        self._pdata_cache = rows
        return list(rows)

    def metadata(self):
        return {"path": str(self.path), "sha256": self.sha256, "layout": self.layout,
                "layout_evidence": self.layout_evidence, "base": self.base,
                "machine": self.machine, "size": len(self.data),
                "size_of_image": self.size_of_image, "entrypoint": self.entrypoint,
                "sections": self.sections, "directories": self.directories,
                "holds": self.holds}
