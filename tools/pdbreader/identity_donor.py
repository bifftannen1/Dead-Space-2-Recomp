"""Lossless, bounded donor-PDB identity extraction; never retail name inference.

Formats: Microsoft's cvinfo.h (CodeView leaf/symbol definitions), and LLVM's
PDB MSF/DBI/TPI documentation. No compiler, symbol server, or third-party parser
is used. Unknown records and unfinished decodes remain raw, explicit holds.
https://github.com/microsoft/microsoft-pdb/blob/master/include/cvinfo.h
https://llvm.org/docs/PDB/MsfFile.html
https://llvm.org/docs/PDB/DbiStream.html
https://llvm.org/docs/PDB/TpiStream.html

Symbols with matching GUID/age belong to the input donor image. They do not
automatically name another image. A public symbol has no manufactured extent.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path
import struct
import sys
import uuid


class DonorError(ValueError):
    """A structural/identity gate failed; no usable donor should be exported."""


def hx(value):
    return "0x%08X" % value


def checked(data, off, size, label="record"):
    if off < 0 or size < 0 or off + size > len(data):
        raise DonorError("truncated %s at %d: need %d, available %d" %
                         (label, off, size, max(0, len(data) - off)))
    return data[off:off + size]


class Reader:
    def __init__(self, data, offset=0):
        self.data, self.pos = data, offset
        self.names = []

    def take(self, size):
        value = checked(self.data, self.pos, size)
        self.pos += size
        return value

    def unpack(self, fmt):
        return struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))

    def u8(self):
        return self.unpack("B")[0]

    def u16(self):
        return self.unpack("H")[0]

    def u32(self):
        return self.unpack("I")[0]

    def i32(self):
        return self.unpack("i")[0]

    def name(self, pascal=False):
        if pascal:
            raw = self.take(self.u8())
        else:
            end = self.data.find(b"\0", self.pos)
            if end < 0:
                raise DonorError("unterminated CodeView name at %d" % self.pos)
            raw = self.take(end - self.pos)
            self.take(1)
        # Replacement is only presentation: original record bytes are retained.
        value = raw.decode("utf-8", "replace")
        self.names.append((value, raw))
        return value

    def numeric(self):
        value, self.pos = decode_numeric(self.data, self.pos)
        return value


# Numeric leaf sizes are cvinfo.h LEAF_ENUM_e / lf* numeric structures.
_NUMERIC_INTS = {0x8000: (1, True), 0x8001: (2, True), 0x8002: (2, False),
                 0x8003: (4, True), 0x8004: (4, False),
                 0x8009: (8, True), 0x800A: (8, False),
                 0x8017: (16, True), 0x8018: (16, False)}
_NUMERIC_OPAQUE = {0x8005: 4, 0x8006: 8, 0x8007: 10, 0x8008: 16,
                   0x800B: 6, 0x800C: 8, 0x800D: 16, 0x800E: 20,
                   0x800F: 32, 0x8019: 16, 0x801A: 8, 0x801C: 2}


def decode_numeric(data, offset=0):
    """Return (lossless numeric value record, next offset), including signedness.

    Non-integral payloads retain bit-exact bytes, not lossy float conversions.
    Unknown numeric kinds fail the containing decode rather than guessing size.
    """
    r = Reader(data, offset)
    leaf = r.u16()
    out = {"leaf": "0x%04X" % leaf}
    if leaf < 0x8000:
        out.update(value=leaf, encoding="immediate_u16")
    elif leaf in _NUMERIC_INTS:
        size, signed = _NUMERIC_INTS[leaf]
        payload = r.take(size)
        out.update(value=int.from_bytes(payload, "little", signed=signed),
                   signed=signed, bits=size * 8, encoding="integer")
    elif leaf in _NUMERIC_OPAQUE:
        out.update(encoding="noninteger_bits", payload_hex=r.take(_NUMERIC_OPAQUE[leaf]).hex())
    elif leaf == 0x8010:  # LF_VARSTRING
        out.update(encoding="bytes", payload_hex=r.take(r.u16()).hex())
    elif leaf == 0x801B:  # LF_UTF8STRING: null-terminated UTF-8
        out.update(encoding="utf8", value=r.name())
    else:
        raise DonorError("unsupported numeric leaf 0x%04X" % leaf)
    out["raw_hex"] = data[offset:r.pos].hex()
    return out, r.pos


class MSF:
    """MSF 7.00 reader with stream framing and physical-block provenance."""
    MAGIC = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\0\0\0"
    MAX_FILE_BYTES = 2 * 1024 ** 3
    MAX_STREAMS = 1000000

    def __init__(self, path):
        self.path = str(Path(path).resolve())
        size = Path(path).stat().st_size
        if size > self.MAX_FILE_BYTES:
            raise DonorError("resource bound: PDB exceeds 2 GiB")
        self.data = Path(path).read_bytes()
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        if checked(self.data, 0, 32, "MSF magic") != self.MAGIC:
            raise DonorError("unsupported PDB: expected MSF 7.00")
        self.block_size, _, self.block_count, directory_size, _, block_map = struct.unpack(
            "<6I", checked(self.data, 32, 24, "MSF superblock"))
        if self.block_size not in (512, 1024, 2048, 4096, 8192, 16384, 32768):
            raise DonorError("unsupported MSF block size %d" % self.block_size)
        if self.block_count * self.block_size > len(self.data):
            raise DonorError("truncated MSF physical blocks")
        count = (directory_size + self.block_size - 1) // self.block_size
        # The directory block map is a contiguous u32 array starting at block `block_map`.
        # It occupies one block for 4 KiB-block PDBs but spans several consecutive blocks
        # for 1 KiB-block PDBs (e.g. Dead Space 3 2012: 387 directory blocks * 4 B > 1024 B).
        # Correctness of the multi-block read is gated downstream by the PE RSDS GUID/age
        # match; a mis-read directory cannot reproduce the declared GUID.
        map_blocks = (count * 4 + self.block_size - 1) // self.block_size
        if block_map + map_blocks > self.block_count:
            raise DonorError("directory block map exceeds MSF physical size")
        map_bytes = b"".join(self.block(block_map + i) for i in range(map_blocks))
        blocks = struct.unpack("<%dI" % count, map_bytes[:count * 4])
        directory = b"".join(self.block(b) for b in blocks)[:directory_size]
        dr = Reader(directory)
        ns = dr.u32()
        if ns > self.MAX_STREAMS:
            raise DonorError("resource bound: too many MSF streams")
        self.sizes = [dr.u32() for _ in range(ns)]
        self.blocks = []
        for size in self.sizes:
            nb = 0 if size == 0xFFFFFFFF else (size + self.block_size - 1) // self.block_size
            if nb > self.block_count:
                raise DonorError("stream size exceeds MSF physical size")
            sb = [dr.u32() for _ in range(nb)]
            if any(b >= self.block_count for b in sb):
                raise DonorError("out-of-range MSF stream block")
            self.blocks.append(sb)
        if dr.pos != len(directory):
            raise DonorError("unaccounted MSF directory bytes")

    def block(self, number):
        if number >= self.block_count:
            raise DonorError("out-of-range MSF block %d" % number)
        return checked(self.data, number * self.block_size, self.block_size, "MSF block")

    def stream(self, index, optional=False):
        if index in (-1, 0xFFFF, 0xFFFFFFFF) or index >= len(self.sizes):
            if optional:
                return b""
            raise DonorError("missing MSF stream %d" % index)
        if self.sizes[index] == 0xFFFFFFFF:
            if optional:
                return b""
            raise DonorError("nil MSF stream %d" % index)
        return b"".join(self.block(b) for b in self.blocks[index])[:self.sizes[index]]

    def provenance(self, index, offset, size):
        chunks = []
        remaining = size
        while remaining:
            block_index, within = divmod(offset, self.block_size)
            if block_index >= len(self.blocks[index]):
                raise DonorError("record exceeds stream block list")
            take = min(remaining, self.block_size - within)
            chunks.append({"file_offset": self.blocks[index][block_index] * self.block_size + within,
                           "size": take})
            offset += take
            remaining -= take
        return chunks


def records(data, start=0):
    """Strict length-prefixed CodeView records; never silently truncate."""
    p = start
    while p < len(data):
        length, kind = struct.unpack("<HH", checked(data, p, 4, "CodeView record header"))
        if length < 2:
            raise DonorError("invalid CodeView record length %d at %d" % (length, p))
        raw = checked(data, p, length + 2, "CodeView record body")
        yield p, kind, raw[4:], raw
        p += length + 2


TYPE_NAMES = {0x1001: "LF_MODIFIER", 0x1002: "LF_POINTER", 0x1008: "LF_PROCEDURE",
              0x1009: "LF_MFUNCTION", 0x1201: "LF_ARGLIST", 0x1203: "LF_FIELDLIST",
              0x1205: "LF_BITFIELD", 0x1206: "LF_METHODLIST", 0x1503: "LF_ARRAY",
              0x1504: "LF_CLASS", 0x1505: "LF_STRUCTURE", 0x1506: "LF_UNION",
              0x1507: "LF_ENUM", 0x1519: "LF_INTERFACE", 0x000A: "LF_VTSHAPE",
              0x150A: "LF_ALIAS", 0x1601: "LF_FUNC_ID", 0x1602: "LF_MFUNC_ID",
              0x1603: "LF_BUILDINFO", 0x1604: "LF_SUBSTR_LIST", 0x1605: "LF_STRING_ID",
              0x1606: "LF_UDT_SRC_LINE", 0x1607: "LF_UDT_MOD_SRC_LINE"}
FIELD_NAMES = {0x1400: "LF_BCLASS", 0x1401: "LF_VBCLASS", 0x1402: "LF_IVBCLASS",
               0x1404: "LF_INDEX", 0x1409: "LF_VFUNCTAB", 0x140A: "LF_FRIENDCLS",
               0x140C: "LF_VFUNCOFF", 0x1502: "LF_ENUMERATE", 0x150C: "LF_FRIENDFCN",
               0x150D: "LF_MEMBER", 0x150E: "LF_STMEMBER", 0x150F: "LF_METHOD",
               0x1510: "LF_NESTTYPE", 0x1511: "LF_ONEMETHOD", 0x1512: "LF_NESTTYPEEX",
               0x1513: "LF_MEMBERMODIFY", 0x151A: "LF_BINTERFACE"}


def padding(r):
    """Consume only specified CodeView LF_PADn descending padding sequences."""
    if r.pos >= len(r.data) or r.data[r.pos] < 0xF0:
        return False
    n = r.data[r.pos] & 15
    if not n or r.take(n) != bytes(range(0xF0 + n, 0xF0, -1)):
        raise DonorError("invalid CodeView padding")
    return True


def attributes(attr):
    return {"raw": attr, "access": attr & 3, "method_property": (attr >> 2) & 7,
            "pseudo": bool(attr & 0x20), "noinherit": bool(attr & 0x40),
            "noconstruct": bool(attr & 0x80), "compiler_generated": bool(attr & 0x100)}


def decode_field_list(body):
    r, fields, refs = Reader(body), [], []
    while r.pos < len(body):
        start = r.pos
        try:
            if padding(r):
                continue
            kind = r.u16()
            row = {"kind": FIELD_NAMES.get(kind, "0x%04X" % kind), "offset": start}
            if kind in (0x1400, 0x151A, 0x1401, 0x1402, 0x150D, 0x150E, 0x1511, 0x1512, 0x1513):
                attr, typ = r.u16(), r.u32()
                row.update(attributes=attributes(attr), type_index=hx(typ))
                refs.append(hx(typ))
                if kind in (0x1400, 0x151A, 0x150D):
                    row["member_offset"] = r.numeric()
                elif kind in (0x1401, 0x1402):
                    typ = r.u32()
                    refs.append(hx(typ))
                    row.update(vbptr_type=hx(typ), vbptr_offset=r.numeric(), vbase_offset=r.numeric())
                elif kind == 0x1511 and (attr >> 2) & 7 in (4, 6):
                    row["vtable_offset"] = r.i32()
                if kind in (0x150D, 0x150E, 0x1511, 0x1512, 0x1513):
                    row["name"] = r.name()
            elif kind in (0x1404, 0x1409, 0x140A, 0x140C, 0x1510, 0x150C):
                pad, typ = r.u16(), r.u32()
                if pad:
                    raise DonorError("nonzero internal field padding")
                row["type_index"] = hx(typ)
                refs.append(hx(typ))
                if kind == 0x1404:
                    row["continuation"] = hx(typ)
                if kind == 0x140C:
                    row["member_offset"] = r.i32()
                if kind in (0x1510, 0x150C):
                    row["name"] = r.name()
            elif kind == 0x150F:
                count, typ = r.u16(), r.u32()
                row.update(count=count, method_list=hx(typ), name=r.name())
                refs.append(hx(typ))
            elif kind == 0x1502:
                row.update(attributes=attributes(r.u16()), value=r.numeric(), name=r.name())
            else:
                raise DonorError("unsupported field leaf 0x%04X" % kind)
            row["raw_hex"] = body[start:r.pos].hex()
            fields.append(row)
        except DonorError as exc:
            return {"fields": fields, "references": refs, "decode_status": "HOLD_PARTIAL",
                    "decode_error": str(exc), "opaque_tail_offset": start,
                    "opaque_tail_hex": body[start:].hex()}
    return {"fields": fields, "references": refs, "decode_status": "DECODED"}


def decode_type(kind, body):
    """Decode supported modern 32-bit-index records; raw bytes always retained."""
    out = {"kind": TYPE_NAMES.get(kind, "0x%04X" % kind), "leaf": "0x%04X" % kind,
           "decode_status": "DECODED", "references": []}
    r = Reader(body)
    refs = out["references"]

    def ref():
        value = hx(r.u32())
        refs.append(value)
        return value

    try:
        if kind == 0x1008:
            out.update(return_type=ref(), calling_convention=r.u8(), function_attributes=r.u8(),
                       parameter_count=r.u16(), argument_list=ref())
        elif kind == 0x1009:
            out.update(return_type=ref(), class_type=ref(), this_type=ref(), calling_convention=r.u8(),
                       function_attributes=r.u8(), parameter_count=r.u16(), argument_list=ref(),
                       this_adjustment=r.i32())
        elif kind == 0x1201:
            count = r.u32()
            if count > (len(body) - r.pos) // 4:
                raise DonorError("argument count exceeds record")
            out.update(count=count, arguments=[ref() for _ in range(count)])
        elif kind == 0x1001:
            out.update(modified_type=ref(), modifiers=r.u16())
        elif kind == 0x1002:
            out.update(referent_type=ref(), attributes=r.u32())
            attr = out["attributes"]
            out.update(pointer_kind=attr & 31, pointer_mode=(attr >> 5) & 7,
                       size=(attr >> 13) & 63)
            if out["pointer_mode"] in (2, 3):
                out.update(containing_class=ref(), member_representation=r.u16())
            elif out["pointer_kind"] not in (0, 1, 2, 10, 11, 12):
                # Based pointer variants carry union-specific data. Preserve it.
                raise DonorError("based-pointer payload is not yet decoded")
        elif kind in (0x1504, 0x1505, 0x1519, 0x1506, 0x1507):
            out.update(member_count=r.u16(), properties=r.u16())
            if kind == 0x1507:
                out.update(underlying_type=ref(), field_list=ref())
            else:
                out["field_list"] = ref()
                if kind != 0x1506:
                    out.update(derived_list=ref(), vshape=ref())
                out["size"] = r.numeric()
            out["name"] = r.name()
            out["forward_reference"] = bool(out["properties"] & 0x80)
            if out["properties"] & 0x200:
                out["unique_name"] = r.name()
        elif kind == 0x1503:
            out.update(element_type=ref(), index_type=ref(), size=r.numeric(), name=r.name())
        elif kind == 0x1205:
            out.update(underlying_type=ref(), bit_length=r.u8(), bit_position=r.u8())
        elif kind == 0x1203:
            out.update(decode_field_list(body))
            r.pos = len(body)
        elif kind == 0x1206:
            methods = []
            while r.pos < len(body):
                if padding(r):
                    continue
                start = r.pos
                attr, pad = r.u16(), r.u16()
                if pad:
                    raise DonorError("nonzero method-list padding")
                method = {"attributes": attributes(attr), "type_index": ref()}
                if (attr >> 2) & 7 in (4, 6):
                    method["vtable_offset"] = r.i32()
                method["raw_hex"] = body[start:r.pos].hex()
                methods.append(method)
            out["methods"] = methods
        elif kind == 0x000A:
            count = r.u16()
            packed = r.take((count + 1) // 2)
            out.update(count=count, descriptors=[(packed[i // 2] >> (4 if i % 2 == 0 else 0)) & 15
                                                 for i in range(count)])
        elif kind == 0x150A:
            out.update(underlying_type=ref(), name=r.name())
        elif kind in (0x1601, 0x1602):
            out.update(parent_scope=ref(), function_type=ref(), name=r.name())
        elif kind == 0x1605:
            out.update(substring_list=ref(), name=r.name())
        elif kind in (0x1603, 0x1604):
            count = r.u16() if kind == 0x1603 else r.u32()
            if count > (len(body) - r.pos) // 4:
                raise DonorError("ID item count exceeds record")
            out.update(count=count, items=[ref() for _ in range(count)])
        elif kind in (0x1606, 0x1607):
            out.update(udt=ref(), source_file=ref(), line=r.u32())
            if kind == 0x1607:
                out["module"] = r.u16()
        else:
            out.update(decode_status="HOLD_OPAQUE", opaque_tail_offset=0, opaque_tail_hex=body.hex())
            return out
        while r.pos < len(body) and padding(r):
            pass
        if r.pos != len(body):
            raise DonorError("unparsed type payload")
    except DonorError as exc:
        out.update(decode_status="HOLD_PARTIAL", decode_error=str(exc),
                   opaque_tail_offset=r.pos, opaque_tail_hex=body[r.pos:].hex())
    if "name" in out:
        raw_name = next((raw for value, raw in r.names if value == out["name"]), None)
        if raw_name is not None:
            out["name_raw_hex"] = raw_name.hex()
            out["name_lossless_utf8"] = raw_name.decode("utf-8", "replace").encode("utf-8") == raw_name
    return out


TPI_VERSIONS = {19950410, 19951122, 19961031, 19990903, 20040203}


def type_header(data):
    version, header_size, first, limit, byte_count = struct.unpack("<5I", checked(data, 0, 20, "TPI header"))
    if version not in TPI_VERSIONS or header_size < 56 or header_size > len(data):
        raise DonorError("not a supported TPI/IPI header")
    if first < 0x1000 or limit < first or limit - first > 4000000:
        raise DonorError("invalid/resource-bound TPI type-index range")
    checked(data, header_size, byte_count, "TPI record area")
    return {"version": version, "header_size": header_size, "first_index": hx(first),
            "end_index": hx(limit), "record_bytes": byte_count,
            "raw_header_hex": data[:header_size].hex()}


def parse_types(data, stream=2):
    header = type_header(data)
    first, limit = int(header["first_index"], 16), int(header["end_index"], 16)
    start = header["header_size"]
    result = {}
    for ordinal, (off, kind, body, raw) in enumerate(records(data[start:start + header["record_bytes"]])):
        index = first + ordinal
        if index >= limit:
            raise DonorError("TPI record count exceeds declared index range")
        row = decode_type(kind, body)
        row.update(type_index=hx(index), stream=stream, stream_offset=start + off, raw_hex=raw.hex())
        result[hx(index)] = row
    if len(result) != limit - first:
        raise DonorError("TPI record count does not match declared index range")
    header["trailing_bytes"] = len(data) - start - header["record_bytes"]
    if header["trailing_bytes"]:
        header["trailing_raw_hex"] = data[start + header["record_bytes"]:].hex()
    return result, header


SYMBOL_NAMES = {0x110E: "public", 0x110C: "local_data", 0x110D: "global_data",
                0x1112: "local_thread_data", 0x1113: "global_thread_data",
                0x110F: "local_procedure", 0x1110: "global_procedure", 0x1102: "thunk",
                0x1146: "local_procedure_id", 0x1147: "global_procedure_id",
                0x1105: "label", 0x1103: "block", 0x1108: "udt", 0x1107: "constant",
                0x1106: "register", 0x110B: "base_pointer_relative", 0x1111: "register_relative",
                0x113E: "local", 0x1125: "procedure_reference", 0x1127: "local_procedure_reference",
                0x1126: "data_reference", 0x1129: "trampoline", 0x114D: "inline_site",
                0x115D: "inline_site2", 0x0006: "end", 0x114E: "inline_site_end",
                0x1012: "frame_procedure", 0x1101: "object_name", 0x1116: "compile2",
                0x113C: "compile3", 0x113D: "environment", 0x1128: "namespace",
                0x1136: "section", 0x1137: "coff_group", 0x1138: "export",
                0x1139: "callsite_info", 0x115E: "heap_alloc_site", 0x1132: "separated_code"}


def decode_symbol(kind, body):
    r = Reader(body)
    out = {"kind": SYMBOL_NAMES.get(kind, "0x%04X" % kind), "symbol_kind": "0x%04X" % kind,
           "decode_status": "DECODED"}
    try:
        if kind in (0x110E, 0x110C, 0x110D, 0x1112, 0x1113):
            value, off, seg = r.unpack("IIH")
            out.update(offset=off, section_index=seg, name=r.name())
            out["public_flags" if kind == 0x110E else "type_index"] = value if kind == 0x110E else hx(value)
        elif kind in (0x110F, 0x1110, 0x1146, 0x1147):
            parent, end, nxt, size, dbeg, dend, typ, off, seg, flags = r.unpack("IIIIIIIIHB")
            out.update(parent=parent, end=end, next=nxt, size=size, debug_start=dbeg, debug_end=dend,
                       type_index=hx(typ), type_index_space="IPI" if kind in (0x1146, 0x1147) else "TPI",
                       offset=off, section_index=seg, flags=flags, name=r.name(), extent_source="pdb_procedure")
        elif kind == 0x1102:
            parent, end, nxt, off, seg, size, ordinal = r.unpack("IIIIHHB")
            out.update(parent=parent, end=end, next=nxt, offset=off, section_index=seg,
                       size=size, ordinal=ordinal, name=r.name(), extent_source="pdb_thunk")
            if r.pos < len(body):
                out["variant_raw_hex"] = body[r.pos:].hex()
                out["variant_decode_status"] = "HOLD_OPAQUE"
                r.pos = len(body)
        elif kind == 0x1105:
            off, seg, flags = r.unpack("IHB")
            out.update(offset=off, section_index=seg, flags=flags, name=r.name())
        elif kind == 0x1103:
            parent, end, size, off, seg = r.unpack("IIIIH")
            out.update(parent=parent, end=end, size=size, offset=off, section_index=seg,
                       name=r.name(), extent_source="pdb_block_not_function")
        elif kind == 0x1108:
            out.update(type_index=hx(r.u32()), name=r.name())
        elif kind == 0x1107:
            out.update(type_index=hx(r.u32()), value=r.numeric(), name=r.name())
        elif kind == 0x1106:
            out.update(type_index=hx(r.u32()), register=r.u16(), name=r.name())
        elif kind == 0x110B:
            out.update(frame_offset=r.i32(), type_index=hx(r.u32()), name=r.name())
        elif kind == 0x1111:
            out.update(register_offset=r.i32(), type_index=hx(r.u32()), register=r.u16(), name=r.name())
        elif kind == 0x113E:
            out.update(type_index=hx(r.u32()), flags=r.u16(), name=r.name())
        elif kind in (0x1125, 0x1126, 0x1127):
            out.update(name_checksum=r.u32(), referenced_symbol_offset=r.u32(),
                       referenced_module=r.u16(), name=r.name())
        elif kind == 0x1129:
            typ, size, off, target, seg, tseg = r.unpack("HHIIHH")
            out.update(trampoline_type=typ, size=size, offset=off, section_index=seg,
                       target_offset=target, target_section_index=tseg, extent_source="pdb_trampoline")
        elif kind in (0x114D, 0x115D):
            out.update(parent=r.u32(), end=r.u32(), inlinee_id=hx(r.u32()))
            if kind == 0x115D:
                out["invocations"] = r.u32()
            out["binary_annotations_hex"] = body[r.pos:].hex()
            out["annotation_decode_status"] = "HOLD_OPAQUE"
            r.pos = len(body)
        elif kind == 0x1012:
            frame, pad, pad_offset, saved, eh_offset, eh_section, flags = r.unpack("IIIIIHI")
            out.update(frame_size=frame, frame_padding=pad, padding_offset=pad_offset,
                       saved_register_bytes=saved, exception_handler_offset=eh_offset,
                       exception_handler_section=eh_section, flags=flags,
                       has_eh=bool(flags & 0x10), inline_specified=bool(flags & 0x20),
                       was_inlined=bool(flags & 0x800), optimize_for_speed=bool(flags & 0x100000))
        elif kind == 0x1101:
            out.update(signature=r.u32(), name=r.name())
        elif kind in (0x1116, 0x113C):
            flags, machine = r.u32(), r.u16()
            count = 4 if kind == 0x113C else 3
            out.update(flags=flags, language=flags & 255, machine=machine,
                       frontend_version=[r.u16() for _ in range(count)],
                       backend_version=[r.u16() for _ in range(count)], compiler_version=r.name(),
                       no_debug_info=bool(flags & 0x200), ltcg=bool(flags & 0x400))
            if kind == 0x1116:
                strings = []
                while r.pos < len(body) and any(body[r.pos:]):
                    strings.append(r.name())
                out["extra_strings"] = strings
        elif kind == 0x113D:
            out["flags"] = r.u8()
            strings = []
            while r.pos < len(body) and any(body[r.pos:]):
                strings.append(r.name())
            out["strings"] = strings
        elif kind == 0x1128:
            out["name"] = r.name()
        elif kind == 0x1136:
            section, align, reserved, rva, size, chars = r.unpack("HBBIII")
            out.update(declared_section=section, alignment_power=align, reserved=reserved,
                       declared_rva=hx(rva), size=size, characteristics=chars, name=r.name())
        elif kind == 0x1137:
            size, chars, off, seg = r.unpack("IIIH")
            out.update(size=size, characteristics=chars, offset=off, section_index=seg,
                       name=r.name(), extent_source="pdb_coff_group_not_function")
        elif kind == 0x1138:
            out.update(ordinal=r.u16(), flags=r.u16(), name=r.name())
        elif kind in (0x1139, 0x115E):
            off, seg, instr_or_pad, typ = r.unpack("IHHI")
            out.update(offset=off, section_index=seg, type_index=hx(typ))
            if kind == 0x1139 and instr_or_pad:
                raise DonorError("nonzero callsite-info alignment padding")
            if kind == 0x115E:
                out["instruction_size"] = instr_or_pad
        elif kind == 0x1132:
            parent, end, size, flags, off, parent_off, seg, parent_seg = r.unpack("IIIIIIHH")
            out.update(parent=parent, end=end, size=size, flags=flags, offset=off, section_index=seg,
                       parent_offset=parent_off, parent_section_index=parent_seg,
                       extent_source="pdb_separated_code", returns_to_parent=bool(flags & 2))
        elif kind in (0x0006, 0x114E):
            pass
        else:
            out.update(decode_status="HOLD_OPAQUE", opaque_tail_offset=0, opaque_tail_hex=body.hex())
            return out
        # Symbol alignment is zero padding, unlike LF_PADn in type records.
        tail = body[r.pos:]
        if tail and any(tail):
            raise DonorError("unparsed symbol payload")
    except DonorError as exc:
        out.update(decode_status="HOLD_PARTIAL", decode_error=str(exc),
                   opaque_tail_offset=r.pos, opaque_tail_hex=body[r.pos:].hex())
    if "name" in out:
        raw_name = next((raw for value, raw in r.names if value == out["name"]), None)
        if raw_name is not None:
            out["name_raw_hex"] = raw_name.hex()
            out["name_lossless_utf8"] = raw_name.decode("utf-8", "replace").encode("utf-8") == raw_name
    return out


def pe_codeview(image):
    """Read only declared IMAGE_DEBUG_DIRECTORY CodeView entries, no RSDS grep."""
    entry = next((e for e in image.directories if e["index"] == 6), None)
    if not entry or not entry["rva"] or not entry["size"]:
        raise DonorError("PE has no declared debug directory")
    if entry["size"] % 28:
        raise DonorError("malformed PE debug directory length")
    data = image.read_va(image.base + entry["rva"], entry["size"])
    result = []
    for off in range(0, len(data), 28):
        _, timestamp, major, minor, typ, size, rva, raw = struct.unpack_from("<IIHHIIII", data, off)
        if typ != 2:
            continue
        if rva:
            payload = image.read_va(image.base + rva, size)
        elif image.layout == "raw":
            payload = checked(image.data, raw, size, "raw CodeView payload")
        else:
            raise DonorError("mapped CodeView entry has no RVA")
        row = {"debug_directory_offset": off, "rva": hx(rva), "raw_file_pointer": raw,
               "timestamp": timestamp, "version": [major, minor], "raw_hex": payload.hex()}
        if payload.startswith(b"RSDS"):
            checked(payload, 0, 24, "RSDS record")
            row.update(signature="RSDS", guid=str(uuid.UUID(bytes_le=payload[4:20])),
                       age=struct.unpack_from("<I", payload, 20)[0], pdb_path=Reader(payload, 24).name())
        elif payload.startswith(b"NB10"):
            # CV_INFO_PDB20: signature, offset, PDB signature/timestamp, age,
            # then a NUL-terminated path.  This authenticates a PDB 2.0 by
            # signature+age; unlike RSDS it contains no GUID.
            checked(payload, 0, 16, "NB10 record")
            offset, pdb_signature, age = struct.unpack_from("<III", payload, 4)
            row.update(signature="NB10", offset=offset,
                       pdb_signature="0x%08X" % pdb_signature, age=age,
                       pdb_path=Reader(payload, 16).name(),
                       identity_kind="PDB20_SIGNATURE_AGE_NO_GUID")
        else:
            row.update(signature=payload[:4].hex(), decode_status="HOLD_UNSUPPORTED_CODEVIEW")
        result.append(row)
    return result


def validate_identity(image, msf):
    info = msf.stream(1)
    version, signature, age = struct.unpack("<III", checked(info, 0, 12, "PDB info header"))
    guid = str(uuid.UUID(bytes_le=checked(info, 12, 16, "PDB GUID")))
    cv = pe_codeview(image)
    if not any(row.get("guid") == guid and row.get("age") == age for row in cv):
        raise DonorError("PDB_IDENTITY_FAIL: declared PE CodeView GUID/age does not match PDB")
    return {"path": msf.path, "sha256": msf.sha256, "version": version, "signature": signature,
            "guid": guid, "age": age, "guid_age_match": True, "codeview_records": cv}


def parse_dbi(msf):
    data = msf.stream(3)
    values = struct.unpack("<iIIHHHHHHiiiiiIiiHHI", checked(data, 0, 64, "DBI header"))
    (sig, ver, age, gsi, build, psi, dllver, symstream, rebuild, modsize, scsize,
     mapsize, sourcesize, typeserversize, mfc, debugsize, ecsize, flags, machine, reserved) = values
    if sig != -1 or ver < 19990903:
        raise DonorError("unsupported DBI version/header")
    sizes = [modsize, scsize, mapsize, sourcesize, typeserversize, ecsize, debugsize]
    if any(v < 0 for v in sizes):
        raise DonorError("negative DBI substream size")
    if sum(sizes) + 64 > len(data):
        raise DonorError("DBI substreams exceed stream size")
    mr = Reader(data[64:64 + modsize])
    modules = []
    while mr.pos < len(mr.data):
        start = mr.pos
        fixed = mr.take(64)
        stream = struct.unpack_from("<H", fixed, 34)[0]
        symbol_bytes, c11, c13 = struct.unpack_from("<III", fixed, 36)
        module, obj = mr.name(), mr.name()
        mr.take((-mr.pos) % 4)
        modules.append({"index": len(modules), "module": module, "object": obj,
                        "stream": stream, "symbol_bytes": symbol_bytes,
                        "c11_bytes": c11, "c13_bytes": c13, "source_file_count": struct.unpack_from("<H", fixed, 48)[0],
                        "stream_offset": 64 + start, "raw_hex": mr.data[start:mr.pos].hex()})
    offset = 64 + modsize
    substreams = {}
    for name, size in zip(("section_contributions", "section_map", "source_info", "type_server_map", "ec", "optional_debug"), sizes[1:]):
        substreams[name] = {"stream_offset": offset, "size": size, "raw_hex": data[offset:offset + size].hex()}
        offset += size
    if offset < len(data):
        substreams["trailing"] = {"stream_offset": offset, "size": len(data) - offset, "raw_hex": data[offset:].hex()}
    return {"version": ver, "age": age, "machine": hx(machine), "symbol_stream": symstream,
            "global_index_stream": gsi, "public_index_stream": psi, "flags": flags,
            "raw_header_hex": data[:64].hex(), "substreams": substreams}, modules


CONTRIBUTION_SCHEMA = "identity-section-contribution-v2"


def migrate_donor_contributions(donor):
    """Return a v2 donor with byte-checked zero-based contribution indices.

    v1's ``module_index_1based`` label was wrong: its value was the unchanged
    DBI SectionContrib Imod u16 at byte +16, matching zero-based module records.
    Never subtract one. See LLVM DbiStream.html#section-contribution-substream
    and the live QA PDB's 40,883 exact-start procedure/contribution joins.
    Only contribution dictionaries and the outer donor are copied; unrelated
    payloads are retained by reference and are not mutated by this helper.
    This migrates a schema, not authenticates the supplying PDB or other fields.
    """
    schema = donor.get("schema")
    if schema not in ("identity-donor-v1", "identity-donor-v2"):
        raise DonorError("unsupported donor contribution migration schema")
    if schema == "identity-donor-v2" and donor.get("section_contribution_schema") != CONTRIBUTION_SCHEMA:
        raise DonorError("missing/unsupported v2 contribution schema")
    rows = donor.get("section_contributions")
    if not isinstance(rows, list):
        raise DonorError("missing contribution records for schema migration")
    migrated = []
    field = "module_index_1based" if schema == "identity-donor-v1" else "module_index_0based"
    other = "module_index_0based" if schema == "identity-donor-v1" else "module_index_1based"
    for row in rows:
        if not isinstance(row, dict) or field not in row or other in row:
            raise DonorError("missing/conflicting contribution module-index fields")
        if schema == "identity-donor-v2" and row.get("schema") != CONTRIBUTION_SCHEMA:
            raise DonorError("unsupported contribution record schema")
        if schema == "identity-donor-v1" and "schema" in row:
            raise DonorError("unexpected versioned contribution record inside v1 donor")
        try:
            raw = bytes.fromhex(row["raw_hex"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DonorError("missing/invalid contribution raw bytes") from exc
        if len(raw) not in (28, 32):
            raise DonorError("unsupported contribution raw record size")
        value = row[field]
        if type(value) is not int or not 0 <= value <= 0xffff or value != struct.unpack_from("<H", raw, 16)[0]:
            raise DonorError("contribution module index differs from raw zero-based value")
        changed = dict(row)
        changed.pop("module_index_1based", None)
        changed.update(schema=CONTRIBUTION_SCHEMA, module_index_0based=value)
        migrated.append(changed)
    return {**donor, "schema": "identity-donor-v2", "section_contribution_schema": CONTRIBUTION_SCHEMA,
            "section_contributions": migrated}


def parse_contributions(data, sections, base):
    if not data:
        return [], []
    version = Reader(data).u32()
    sizes = {0xF12EBA2D: 28, 0xF13151E4: 32}
    if version not in sizes:
        return [], [{"kind": "HOLD_SECTION_CONTRIBUTION_VERSION", "version": hx(version), "raw_hex": data.hex()}]
    size = sizes[version]
    if (len(data) - 4) % size:
        raise DonorError("truncated section contribution record")
    result, by_index = [], {s["index"]: s for s in sections}
    for off in range(4, len(data), size):
        raw = data[off:off + size]
        seg, pad, rel, length, attrs, module, pad2, data_crc, reloc_crc = struct.unpack_from("<HHiiIHHII", raw)
        section = by_index.get(seg)
        row = {"schema": CONTRIBUTION_SCHEMA, "section_index": seg, "offset": rel, "size": length, "characteristics": attrs,
               "module_index_0based": module, "data_crc": data_crc, "reloc_crc": reloc_crc,
               "substream_offset": off, "raw_hex": raw.hex(), "va": None}
        if section and rel >= 0 and length >= 0 and rel + length <= max(section["virtual_size"], section["raw_size"]):
            row["va"] = hx(base + section["rva"] + rel)
        else:
            row["address_status"] = "HOLD_SECTION_CONTRIBUTION_RANGE"
        if size == 32:
            row["coff_section_index"] = struct.unpack_from("<I", raw, 28)[0]
        result.append(row)
    return result, []


def parse_source_info(data, modules):
    """DBI File Info: counts are summed, not truncated to header's 16-bit total.

    LLVM DbiStream.html#file-info-substream defines contiguous per-module
    filename-offset groups; ModIndices is retained without guessed semantics.
    """
    if not data:
        return {"module_count": 0, "file_contributions": 0, "unique_files": 0}
    r = Reader(data)
    count, legacy_total = r.u16(), r.u16()
    if count != len(modules):
        raise DonorError("DBI File Info module count disagrees with module records")
    indices = [r.u16() for _ in range(count)]
    counts = [r.u16() for _ in range(count)]
    total = sum(counts)
    if total > (len(data) - r.pos) // 4:
        raise DonorError("DBI File Info offset count exceeds substream")
    offsets = [r.u32() for _ in range(total)]
    names = data[r.pos:]
    unique, ordinal = {}, 0
    for module, files in zip(modules, counts):
        paths = []
        for offset in offsets[ordinal:ordinal + files]:
            if offset not in unique:
                nr = Reader(names, offset)
                name = nr.name()
                raw = nr.names[-1][1]
                unique[offset] = {"name": name, "name_raw_hex": raw.hex(), "names_buffer_offset": offset}
            paths.append(unique[offset])
        module["source_files"] = paths
        module["source_file_index_raw"] = indices[module["index"]]
        ordinal += files
    return {"module_count": count, "legacy_16bit_file_count": legacy_total,
            "file_contributions": total, "unique_files": len(unique), "names_buffer_offset": r.pos,
            "files": [unique[k] for k in sorted(unique)]}


def resolve_symbol_address(row, sections, base):
    """Do not silently discard non-pdata symbols or out-of-section records."""
    if "section_index" not in row:
        return
    section = next((s for s in sections if s["index"] == row["section_index"]), None)
    off = row["offset"]
    if not section or off >= max(section["virtual_size"], section["raw_size"]):
        row.update(va=None, address_status="HOLD_OUTSIDE_DECLARED_SECTION")
        return
    row.update(va=hx(base + section["rva"] + off), section=section["name"], address_status="IN_DECLARED_SECTION")
    if "size" in row and off + row["size"] > max(section["virtual_size"], section["raw_size"]):
        row["extent_status"] = "HOLD_EXTENT_OUTSIDE_SECTION"


def alias_groups(symbols):
    result = collections.defaultdict(set)
    for row in symbols:
        if row.get("va") and row.get("name"):
            spelling = row["name"] if row.get("name_lossless_utf8", True) else "raw-hex:" + row["name_raw_hex"]
            result[row["va"]].add(spelling)
    return {va: sorted(names) for va, names in sorted(result.items())}


def verify_symbol_records(pe, pdb, symbol_records, layout="mapped"):
    """Re-bind candidate export rows to actual authenticated source PDB bytes.

    A JSON hash field is not a symbol certificate. Each supplied row must name
    an actual record boundary in a declared DBI symbol region; both raw bytes
    and decoded export-relevant fields must agree. Arbitrary MSF streams,
    interior offsets, missing provenance, invented public extents, altered
    type indices/names, and modified module identities fail before export.

    Returns a compact PASS report (including live PE/PDB digests), raises
    DonorError on any discrepancy. Does not write files or admit cross-build
    mappings. The validated source PDB remains the authority, not this report.
    """
    try:
        from .identity_image import PEImage
    except ImportError:
        from identity_image import PEImage
    if not isinstance(symbol_records, list) or len(symbol_records) > 4000000:
        raise DonorError("symbol verification requires a bounded list of records")
    image, msf = PEImage(pe, layout=layout), MSF(pdb)
    pdb_info = validate_identity(image, msf)
    dbi, modules = parse_dbi(msf)
    if dbi["age"] != pdb_info["age"] or int(dbi["machine"], 16) != image.machine:
        raise DonorError("symbol verification DBI identity mismatch")
    debugraw = bytes.fromhex(dbi["substreams"]["optional_debug"]["raw_hex"])
    if len(debugraw) % 2:
        raise DonorError("odd optional DBI debug header")
    debugstreams = struct.unpack("<%dH" % (len(debugraw) // 2), debugraw)
    if any(i < len(debugstreams) and debugstreams[i] != 0xFFFF for i in (3, 4)):
        raise DonorError("HOLD_OMAP: source symbol address remapping is unsupported")
    allowed = {}
    if dbi["symbol_stream"] != 0xFFFF:
        allowed[(dbi["symbol_stream"], None)] = (0, None, None)
    for module in modules:
        if module["stream"] != 0xFFFF and module["symbol_bytes"]:
            allowed[(module["stream"], module["index"])] = (4, module["symbol_bytes"], module)
    requests = collections.defaultdict(lambda: collections.defaultdict(list))
    for ordinal, row in enumerate(symbol_records):
        if not isinstance(row, dict):
            raise DonorError("invalid symbol verification row %d" % ordinal)
        for key in ("stream", "stream_offset", "raw_hex", "kind", "symbol_kind"):
            if key not in row:
                raise DonorError("symbol verification row %d lacks %s" % (ordinal, key))
        if not isinstance(row["stream"], int) or not isinstance(row["stream_offset"], int):
            raise DonorError("symbol stream provenance must be integer-valued")
        region = (row["stream"], row.get("module_index"))
        if region not in allowed:
            raise DonorError("symbol verification row %d is not in a declared DBI symbol region" % ordinal)
        try:
            raw = bytes.fromhex(row["raw_hex"])
        except (ValueError, TypeError) as exc:
            raise DonorError("invalid symbol raw_hex at row %d" % ordinal) from exc
        if len(raw) < 4 or len(raw) > 65537:
            raise DonorError("invalid source symbol raw length")
        requests[region][row["stream_offset"]].append((ordinal, row, raw))
    verified, bytes_verified, boundaries_scanned = 0, 0, 0
    # These fields may not be added to a record kind that did not contain them.
    export_fields = {"va", "name", "name_raw_hex", "name_lossless_utf8", "kind", "symbol_kind",
                     "size", "type_index", "type_index_space", "extent_source", "extent_status",
                     "public_flags", "offset", "section_index", "section", "address_status",
                     "module", "module_index", "object", "lexical_owner_symbol_offset",
                     "lexical_owner_kind", "scope_status"}
    sentinel = object()
    for region, wanted in requests.items():
        stream, _ = region
        start, end, module = allowed[region]
        full = msf.stream(stream)
        data = full if end is None else checked(full, 0, end, "verified module symbol region")
        if start and struct.unpack("<I", checked(data, 0, 4, "verified module signature"))[0] != 4:
            raise DonorError("unsupported source module symbol version")
        scopes = []
        for off, kind, body, raw in records(data, start):
            boundaries_scanned += 1
            while scopes and off >= scopes[-1]["end"]:
                scopes.pop()
            expected = None
            is_scope = kind in (0x110F, 0x1110, 0x1146, 0x1147, 0x1102, 0x1103, 0x1132, 0x114D, 0x115D)
            if off in wanted or is_scope:
                expected = decode_symbol(kind, body)
                if scopes:
                    expected["lexical_owner_symbol_offset"] = scopes[-1]["stream_offset"]
                    expected["lexical_owner_kind"] = scopes[-1]["kind"]
                if is_scope:
                    if off < expected.get("end", 0) < len(data):
                        scopes.append(dict(expected, stream_offset=off))
                    else:
                        expected["scope_status"] = "HOLD_INVALID_SCOPE_END"
            if off not in wanted:
                continue
            resolve_symbol_address(expected, image.sections, image.base)
            if module is not None:
                expected.update(module=module["module"], module_index=module["index"], object=module["object"])
            for ordinal, row, claimed_raw in wanted.pop(off):
                if raw != claimed_raw:
                    raise DonorError("symbol verification row %d raw bytes differ from original PDB" % ordinal)
                for field in set(expected) | export_fields:
                    if expected.get(field, sentinel) != row.get(field, sentinel):
                        raise DonorError("symbol verification row %d changed/missing field %s" % (ordinal, field))
                verified += 1
                bytes_verified += len(raw)
        if wanted:
            raise DonorError("symbol verification offset is not an actual record boundary: stream %d offsets %s" %
                             (stream, sorted(wanted)[:8]))
    if verified != len(symbol_records):
        raise DonorError("symbol verification did not account for every requested record")
    return {"verdict": "PASS", "records_verified": verified, "raw_bytes_verified": bytes_verified,
            "record_boundaries_scanned": boundaries_scanned, "pe_sha256": image.sha256,
            "pdb_sha256": msf.sha256, "pdb_guid": pdb_info["guid"], "pdb_age": pdb_info["age"],
            "guid_age_match": True, "original_symbol_bytes_and_fields_verified": True}


def extract_donor(pe, pdb, layout="mapped"):
    try:
        from .identity_image import PEImage
    except ImportError:
        from identity_image import PEImage
    image, msf = PEImage(pe, layout=layout), MSF(pdb)
    pdb_info = validate_identity(image, msf)
    dbi, modules = parse_dbi(msf)
    if dbi["age"] != pdb_info["age"]:
        raise DonorError("PDB DBI age disagrees with PDB info age")
    if int(dbi["machine"], 16) != image.machine:
        raise DonorError("PDB DBI machine disagrees with PE machine")
    types, tpi_header = parse_types(msf.stream(2), 2)
    source_info = parse_source_info(bytes.fromhex(dbi["substreams"]["source_info"]["raw_hex"]), modules)
    holds = []
    ids, ipi = {}, {"present": False}
    stream4 = msf.stream(4, optional=True)
    if stream4:
        try:
            type_header(stream4)
        except DonorError as exc:
            ipi = {"present": False, "stream4_status": "NOT_VALIDATED_AS_IPI", "reason": str(exc),
                   "stream4_size": len(stream4), "stream4_sha256": hashlib.sha256(stream4).hexdigest(),
                   "raw_header_hex": stream4[:56].hex()}
        else:
            ids, ipi = parse_types(stream4, 4)
            ipi["present"] = True
    contributions_raw = bytes.fromhex(dbi["substreams"]["section_contributions"]["raw_hex"])
    contributions, contribution_holds = parse_contributions(contributions_raw, image.sections, image.base)
    holds.extend(contribution_holds)
    symbols = []
    raw_counts = collections.Counter()

    def consume(stream_index, data, start=0, module=None):
        scopes = []
        for off, kind, body, raw in records(data, start):
            row = decode_symbol(kind, body)
            row.update(stream=stream_index, stream_offset=off, raw_hex=raw.hex())
            while scopes and off >= scopes[-1]["end"]:
                scopes.pop()
            if scopes:
                row["lexical_owner_symbol_offset"] = scopes[-1]["stream_offset"]
                row["lexical_owner_kind"] = scopes[-1]["kind"]
            if kind in (0x110F, 0x1110, 0x1146, 0x1147, 0x1102, 0x1103, 0x1132, 0x114D, 0x115D):
                if off < row.get("end", 0) < len(data):
                    scopes.append(row)
                else:
                    row["scope_status"] = "HOLD_INVALID_SCOPE_END"
            if module is not None:
                row.update(module=module["module"], module_index=module["index"], object=module["object"])
            resolve_symbol_address(row, image.sections, image.base)
            symbols.append(row)
            raw_counts[row["symbol_kind"]] += 1

    if dbi["symbol_stream"] != 0xFFFF:
        consume(dbi["symbol_stream"], msf.stream(dbi["symbol_stream"]))
    module_payloads = []
    for module in modules:
        stream = module["stream"]
        if stream == 0xFFFF:
            if module["symbol_bytes"]:
                raise DonorError("module declares symbols without a stream")
            continue
        data = msf.stream(stream)
        symbol_bytes = module["symbol_bytes"]
        checked(data, 0, symbol_bytes, "module symbol region")
        checked(data, symbol_bytes, module["c11_bytes"] + module["c13_bytes"], "module line metadata region")
        if symbol_bytes:
            signature = struct.unpack("<I", checked(data, 0, 4, "module signature"))[0]
            module["signature"] = signature
            if signature != 4:
                holds.append({"kind": "HOLD_MODULE_SYMBOL_VERSION", "module_index": module["index"],
                              "stream": stream, "signature": signature,
                              "raw_hex": data[:symbol_bytes].hex()})
            else:
                consume(stream, data[:symbol_bytes], 4, module)
        # C11/C13 line data and remaining metadata are retained, not miscalled symbols.
        if symbol_bytes < len(data):
            module_payloads.append({"module_index": module["index"], "stream": stream,
                                    "stream_offset": symbol_bytes, "decode_status": "HOLD_OPAQUE_MODULE_METADATA",
                                    "raw_hex": data[symbol_bytes:].hex()})
    aliases = alias_groups(symbols)
    type_holds = [k for k, v in types.items() if v["decode_status"] != "DECODED"]
    id_holds = [k for k, v in ids.items() if v["decode_status"] != "DECODED"]
    symbol_holds = [i for i, s in enumerate(symbols) if s["decode_status"] != "DECODED"]
    address_holds = [i for i, s in enumerate(symbols) if s.get("address_status", "").startswith("HOLD")]
    if type_holds:
        holds.append({"kind": "HOLD_TYPE_DECODING", "count": len(type_holds), "type_indices": type_holds})
    if id_holds:
        holds.append({"kind": "HOLD_ID_DECODING", "count": len(id_holds), "type_indices": id_holds})
    if symbol_holds:
        holds.append({"kind": "HOLD_SYMBOL_DECODING", "count": len(symbol_holds), "symbol_indices": symbol_holds})
    if address_holds:
        holds.append({"kind": "HOLD_SYMBOL_ADDRESS", "count": len(address_holds), "symbol_indices": address_holds})
    if module_payloads:
        holds.append({"kind": "HOLD_MODULE_LINE_AND_OTHER_METADATA", "count": len(module_payloads),
                      "preservation": "module_metadata raw payloads"})
    contribution_range_holds = [i for i, c in enumerate(contributions) if c.get("address_status", "").startswith("HOLD")]
    if contribution_range_holds:
        holds.append({"kind": "HOLD_CONTRIBUTION_ADDRESSES", "count": len(contribution_range_holds),
                      "contribution_indices": contribution_range_holds})
    extent_holds = [i for i, s in enumerate(symbols) if s.get("extent_status", "").startswith("HOLD")]
    if extent_holds:
        holds.append({"kind": "HOLD_SYMBOL_EXTENTS", "count": len(extent_holds), "symbol_indices": extent_holds})
    encoding_holds = [i for i, s in enumerate(symbols) if s.get("name_lossless_utf8") is False]
    if encoding_holds:
        holds.append({"kind": "HOLD_NAME_TEXT_ENCODING", "count": len(encoding_holds),
                      "symbol_indices": encoding_holds, "preservation": "name_raw_hex and raw-hex alias spellings"})
    # PDB section mappings/OMAP may require remapping in other builds. Report them,
    # never silently use an OMAP-oblivious address as an admitted original symbol.
    debugraw = bytes.fromhex(dbi["substreams"]["optional_debug"]["raw_hex"])
    if len(debugraw) % 2:
        raise DonorError("odd-length optional DBI debug header")
    debugstreams = list(struct.unpack("<%dH" % (len(debugraw) // 2), debugraw))
    remap_streams = {str(i): index for i, index in enumerate(debugstreams) if i in (3, 4) and index != 0xFFFF}
    if remap_streams:
        raise DonorError("HOLD_OMAP: donor uses address remapping not implemented; no address labels admitted")
    pdata_result = image.pdata()
    # Shared image API returns a list; tolerate (list, holds) for older callers.
    pdata_rows = pdata_result[0] if isinstance(pdata_result, tuple) else pdata_result
    starts = {hx(int(p["va"], 16) if isinstance(p["va"], str) else p["va"]) for p in pdata_rows}
    named_addresses = set(aliases)
    named_code = {s["va"] for s in symbols if s.get("va") and s.get("name") and
                  next((sec["characteristics"] & 0x20000000 for sec in image.sections if sec["name"] == s.get("section")), 0)}
    procs = [s for s in symbols if s["kind"] in ("local_procedure", "global_procedure", "local_procedure_id", "global_procedure_id")]
    function_kinds = {"local_procedure", "global_procedure", "local_procedure_id", "global_procedure_id", "public", "thunk"}
    function_names = {s["va"] for s in symbols if s.get("va") in named_code and s.get("name") and s["kind"] in function_kinds}
    summary = {"type_records": len(types), "id_records": len(ids), "type_decode_holds": len(type_holds),
               "symbol_records": len(symbols), "symbol_decode_holds": len(symbol_holds),
               "named_symbol_records": sum(bool(s.get("name")) for s in symbols),
               "named_addresses": len(named_addresses), "named_code_addresses": len(named_code),
               "alias_addresses": sum(len(a) > 1 for a in aliases.values()), "procedure_records": len(procs),
               "procedures_with_nonzero_type_index": sum(s.get("type_index") not in (None, hx(0)) for s in procs),
               "public_records": sum(s["kind"] == "public" for s in symbols),
               "thunk_records": sum(s["kind"] == "thunk" for s in symbols),
               "named_code_addresses_outside_pdata_starts": len(named_code - starts),
               "procedure_public_thunk_code_addresses": len(function_names),
               "procedure_public_thunk_code_addresses_outside_pdata_starts": len(function_names - starts),
               "compiler_records": sum(s["kind"] in ("compile2", "compile3") for s in symbols),
               "frame_procedure_records": sum(s["kind"] == "frame_procedure" for s in symbols),
               "frame_records_with_was_inlined_flag": sum(s.get("was_inlined", False) for s in symbols),
               "source_file_contributions": source_info["file_contributions"],
               "unique_source_file_records": source_info["unique_files"],
               "pdata_starts": len(starts), "pdata_starts_with_name": len(starts & named_addresses),
               "pdata_starts_without_name": len(starts - named_addresses), "modules": len(modules),
               "section_contributions": len(contributions), "symbol_kind_counts": dict(sorted(raw_counts.items())),
               "type_kind_counts": dict(sorted(collections.Counter(t["kind"] for t in types.values()).items())),
               "original_name_scope": "input donor image only; no cross-build promotion"}
    image_info = image.metadata()
    image_info["sha256"] = image.sha256
    if image_info.get("holds"):
        holds.append({"kind": "HOLD_IMAGE_METADATA", "image_holds": image_info["holds"]})
    return {"schema": "identity-donor-v2", "verdict": "PASS_WITH_HOLDS" if holds else "PASS",
            "image": image_info, "pdb": pdb_info,
            "types": types, "ids": ids, "type_headers": {"TPI": tpi_header, "IPI": ipi},
            "symbols": symbols, "aliases": aliases, "modules": modules, "dbi": dbi,
            "source_info": source_info,
            "section_contribution_schema": CONTRIBUTION_SCHEMA,
            "section_contributions": contributions, "module_metadata": module_payloads,
            "stream_directory": [{"index": i, "size": size, "blocks": msf.blocks[i]}
                                 for i, size in enumerate(msf.sizes)],
            "holds": holds, "gates": {"pdb_identity": "PASS", "dbi_identity": "PASS",
                                       "raw_record_framing": "PASS", "raw_records_preserved": "PASS",
                                       "all_metadata_decoded": "HOLD" if holds else "PASS"},
            "summary": summary}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pe", required=True)
    ap.add_argument("--pdb", required=True)
    ap.add_argument("--layout", choices=("mapped", "raw", "auto"), default="mapped")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    try:
        result = extract_donor(args.pe, args.pdb, args.layout)
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as out:
            json.dump(result, out, ensure_ascii=True, separators=(",", ":"))
        print(json.dumps({"out": str(path.resolve()), "gates": result["gates"], "summary": result["summary"]}, indent=2))
        return 0
    except (DonorError, ValueError, OSError) as exc:
        print("DONOR_FAIL: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
