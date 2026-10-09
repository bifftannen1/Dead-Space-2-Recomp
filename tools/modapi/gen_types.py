#!/usr/bin/env python3
r"""gen_types.py -- C++ headers for the game's types from a PDB's TPI stream (B3).

usage: python tools/modapi/gen_types.py --pdb "<...>/deadspace_f.pdb" --out build/app_mods/types_gen
                                        [--include REGEX] [--compile-test] [--sample 200]
       python tools/modapi/gen_types.py --selftest

Reads `llvm-pdbutil dump --types` of the PDB (the user's own file; nothing PDB-derived is
committed: the output goes to the build tree) and writes:
  ds2_types.h        namespace ds2::pdb: every named class/struct/union with a full definition
                     (fields at the PDB's exact offsets, multi-byte scalars as ds2::be<T>,
                     pointers as ds2::gptr<T>), every enum (enum class with its underlying
                     type and enumerators) and a <Type>_vtbl struct for every class that
                     introduces virtual functions (slot = vftable offset / 4).
  ds2_types_test.cpp static_assert(sizeof) for every type and offsetof for every field,
                     against the PDB's numbers.
  ds2_types.json     receipt: counts with denominators, refusals, the sample cross-check.

Layout: a type is an alignment-1 union of one anonymous struct per field
(`struct { uint8_t ds2padN[offset]; T field; };`), plus a byte array of the PDB sizeof.
Every field sits at its PDB offset by construction, overlapping members (anonymous unions in
the source) are exact, and sizeof is the PDB sizeof. Base classes are fields named
base_<Type> at their offset. Bitfields share one storage field per offset (bits_<offset>;
the fields and their bit offsets are listed in a comment). A field whose type has no known
size (member function pointers, ...) becomes opaque bytes up to the next field.
Names: `A::B<C>` becomes A__B_C_ (the PDB spelling is in a comment); an identifier that is a
C++ keyword or a macro visible to a mod TU (probed with clang -dM -E over <windows.h>, the
CRT and the STL) gets a trailing _. A name is not a key: when two different full definitions
share a name, neither is emitted under the plain name; both get a __ti<index> suffix.

--compile-test compiles ds2_types_test.cpp with clang++ -std=c++23 (exit 1 on any failure).
--sample N cross-checks N types against a second reader, `llvm-pdbutil pretty
-class-definitions=layout` (DIA, independent of the dump this tool parses): sizeof and every
top-level data member offset must agree.
Prints GEN_TYPES_OK only when every step passed. Output files are only rewritten on change.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

LLVM = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\Llvm\x64\bin")
PDBUTIL = LLVM / "llvm-pdbutil.exe"
CLANG = LLVM / "clang++.exe"
DIA_DIR = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\PrivateAssemblies\amd64")
REPO = Path(__file__).resolve().parents[2]

REC = re.compile(r"^\s*(0x[0-9A-F]+) \| (LF_[A-Z_0-9]+) \[size = \d+\](?: `(.*)`)?\s*$")
MEMBER = re.compile(r"name = `([^`]*)`, Type = (0x[0-9A-F]+)(?: \([^)]*\))?, offset = (\d+)")
PRIM = {  # simple type index -> (c++ type, size)
    0x0003: ("void", 0), 0x0008: ("int32_t", 4), 0x0010: ("int8_t", 1), 0x0011: ("int16_t", 2),
    0x0012: ("int32_t", 4), 0x0013: ("int64_t", 8), 0x0020: ("uint8_t", 1), 0x0021: ("uint16_t", 2),
    0x0022: ("uint32_t", 4), 0x0023: ("uint64_t", 8), 0x0030: ("uint8_t", 1), 0x0040: ("float", 4),
    0x0041: ("double", 8), 0x0070: ("char", 1), 0x0071: ("uint16_t", 2), 0x0074: ("int32_t", 4),
    0x0075: ("uint32_t", 4), 0x0076: ("int64_t", 8), 0x0077: ("uint64_t", 8), 0x0068: ("int8_t", 1),
    0x0069: ("uint8_t", 1), 0x0072: ("int16_t", 2), 0x0073: ("uint16_t", 2), 0x0103: ("uint32_t", 4),
}
CPP_KEYWORDS = set("""alignas alignof and and_eq asm auto bitand bitor bool break case catch char char8_t
char16_t char32_t class compl concept const consteval constexpr constinit const_cast continue co_await
co_return co_yield decltype default delete do double dynamic_cast else enum explicit export extern false
float for friend goto if inline int long mutable namespace new noexcept not not_eq nullptr operator or
or_eq private protected public register reinterpret_cast requires return short signed sizeof static
static_assert static_cast struct switch template this thread_local throw true try typedef typeid typename
union unsigned using virtual void volatile wchar_t while xor xor_eq""".split())
RESERVED: set[str] = set()  # macro names visible to a mod TU (load_reserved)


class Fail(Exception):
    pass


def load_reserved() -> int:
    """Every macro name a mod TU can see: an emitted identifier equal to one would be
    rewritten by the preprocessor, so such identifiers get a trailing _."""
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "probe.cpp"
        f.write_text("#include <windows.h>\n#include <cmath>\n#include <cstddef>\n#include <cstdint>\n"
                     "#include <cstdio>\n#include <cstring>\n#include <string>\n#include <vector>\n"
                     '#include "ds2_be.h"\n', encoding="utf-8")
        p = subprocess.run([str(CLANG), "-std=c++23", "-dM", "-E", f"-I{REPO / 'src' / 'modapi'}", str(f)],
                           capture_output=True, text=True, errors="replace")
    names = set(re.findall(r"^#define (\w+)", p.stdout, re.M))
    if p.returncode != 0 or "INFINITY" not in names or "WINAPI" not in names:
        raise Fail("macro probe failed (positive control: INFINITY and WINAPI must be defined)")
    RESERVED.update(names)
    return len(names)


def ti(s: str) -> int:
    return int(s, 16)


def parse_dump(lines):
    """Records: index -> dict(kind, name, body)."""
    recs, cur = {}, None
    for ln in lines:
        m = REC.match(ln)
        if m:
            cur = {"kind": m.group(2), "name": m.group(3), "body": []}
            recs[ti(m.group(1))] = cur
        elif cur is not None:
            cur["body"].append(ln.rstrip("\n"))
    return recs


def field_list(body):
    """Split an LF_FIELDLIST body into (kind, header line, continuation lines)."""
    out, cur = [], None
    for ln in body:
        m = re.match(r"^\s*- (LF_[A-Z_]+|Method)\b(.*)$", ln)
        if m:
            cur = [m.group(1), m.group(2).strip(), []]
            out.append(cur)
        elif cur is not None:
            cur[2].append(ln.strip())
    return out


def ident(name: str) -> str:
    s = name.replace("::", "__")
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    if not s or s[0].isdigit() or s[0] == "_":
        s = "u" + s  # no leading underscore: _X / __x are reserved and collide with CRT macros
    while s in CPP_KEYWORDS or s in RESERVED:
        s += "_"
    return s


class Model:
    def __init__(self, recs):
        self.recs = recs
        self.udts: dict[int, dict] = {}  # full definitions
        self.enums: dict[int, dict] = {}
        self.refusals: list[str] = []
        self.stats = {}
        self.canon: dict[int, int] = {}

    def body(self, idx):
        return " ".join(self.recs[idx]["body"])

    def resolve_fwd(self, idx):
        r = self.recs.get(idx)
        if not r:
            return idx
        m = re.search(r"forward ref \(-> (0x[0-9A-F]+)\)", " ".join(r["body"]))
        return ti(m.group(1)) if m else idx

    def strip_mod(self, idx):
        while idx >= 0x1000 and self.recs.get(idx, {}).get("kind") == "LF_MODIFIER":
            m = re.search(r"referent = (0x[0-9A-F]+)", self.body(idx))
            idx = ti(m.group(1))
        return idx

    def size_of(self, idx, depth=0):
        idx = self.strip_mod(idx)
        if idx < 0x1000:
            if (idx & 0x0F00) == 0x0400:
                return 4  # 32-bit near pointer to a simple type
            p = PRIM.get(idx)
            return p[1] if p else None
        r = self.recs.get(idx)
        if not r or depth > 64:
            return None
        k, b = r["kind"], " ".join(r["body"])
        if k == "LF_POINTER":
            if "member" in b:
                return None  # member (function) pointer: size depends on the class model
            return 4
        if k == "LF_ARRAY":
            return int(re.search(r"size: (\d+)", b).group(1))
        if k in ("LF_CLASS", "LF_STRUCTURE", "LF_UNION"):
            full = self.resolve_fwd(idx)
            m = re.search(r"sizeof (\d+)", self.body(full))
            sz = int(m.group(1)) if m else None
            return sz if sz else None
        if k == "LF_ENUM":
            full = self.resolve_fwd(idx)
            m = re.search(r"underlying type: (0x[0-9A-F]+)", self.body(full))
            return self.size_of(ti(m.group(1)), depth + 1) if m else None
        if k == "LF_BITFIELD":
            m = re.search(r"type = (0x[0-9A-F]+)", b)
            return self.size_of(ti(m.group(1)), depth + 1)
        return None

    def collect(self, include):
        by_name: dict[str, list[int]] = {}
        nfwd = 0
        for idx, r in self.recs.items():
            if r["kind"] in ("LF_CLASS", "LF_STRUCTURE", "LF_UNION"):
                b = " ".join(r["body"])
                if "forward ref" in b:
                    nfwd += 1
                    continue
                if include and not re.search(include, r["name"] or ""):
                    continue
                by_name.setdefault(r["name"], []).append(idx)
            elif r["kind"] == "LF_ENUM":
                b = " ".join(r["body"])
                if "forward ref" in b or (include and not re.search(include, r["name"] or "")):
                    continue
                self.enums[idx] = {"name": r["name"]}
        dup_same = dup_diff = 0
        for name, idxs in by_name.items():
            sig = {}
            for i in idxs:
                s = self.layout_sig(i)
                sig.setdefault(s, i)
                self.canon[i] = sig[s]
            if len(sig) > 1:
                dup_diff += 1
                for i in sig.values():
                    self.udts[i] = {"name": name, "suffix": f"__ti{i:x}"}
            else:
                if len(idxs) > 1:
                    dup_same += 1
                self.udts[next(iter(sig.values()))] = {"name": name, "suffix": ""}
        self.stats.update(full_definitions=sum(len(v) for v in by_name.values()), forward_refs=nfwd,
                          names=len(by_name), names_repeated_identical=dup_same,
                          names_with_different_definitions=dup_diff)

    def layout_sig(self, idx):
        b = self.body(idx)
        fl = re.search(r"field list: (0x[0-9A-F]+)", b)
        flb = self.recs[ti(fl.group(1))]["body"] if fl else []
        mem = [l.strip() for l in flb if "LF_MEMBER" in l or "LF_BCLASS" in l or "LF_VFUNCTAB" in l]
        sz = re.search(r"sizeof (\d+)", b)
        norm = []  # member names + offsets (type indices differ between repeats of one type)
        for l in mem:
            m = re.search(r"name = `([^`]*)`.*offset = (\d+)", l)
            norm.append(m.groups() if m else re.sub(r"0x[0-9A-F]+", "T", l))
        return (sz.group(1) if sz else "?", tuple(map(str, norm)))


class Emitter:
    def __init__(self, m: Model):
        self.m = m
        self.names: dict[int, str] = {}
        used: dict[str, int] = {}
        for idx, u in sorted(m.udts.items()):
            n = ident(u["name"]) + u["suffix"]
            if n in used:  # sanitization collision: refuse the plain spelling
                n = n + f"__ti{idx:x}"
                m.stats["ident_collisions"] = m.stats.get("ident_collisions", 0) + 1
            used[n] = idx
            self.names[idx] = n
        self.plain = {}  # PDB name -> the single emitted idx (forward refs resolve through it)
        for idx, u in m.udts.items():
            if not u["suffix"]:
                self.plain[u["name"]] = idx
        self.enum_names = {}
        for idx, e in sorted(m.enums.items()):
            n = ident(e["name"])
            if n in used or n in self.enum_names.values():
                n = n + f"__ti{idx:x}"
            self.enum_names[idx] = n
        self.emitted_fields = self.opaque_fields = self.bitfield_units = self.vtbls = 0

    def udt_of(self, idx):
        """Emitted UDT idx for a (possibly forward-ref) type idx, or None."""
        idx = self.m.strip_mod(idx)
        r = self.m.recs.get(idx)
        if not r or r["kind"] not in ("LF_CLASS", "LF_STRUCTURE", "LF_UNION"):
            return None
        full = self.m.resolve_fwd(idx)
        full = self.m.canon.get(full, full)
        if full in self.names:
            return full
        return self.plain.get(r["name"])

    def cpp_type(self, idx, deps):
        """(type spelling, array suffix) for a field of type idx, or None if unsupported."""
        idx = self.m.strip_mod(idx)
        if idx < 0x1000:
            if (idx & 0x0F00) == 0x0400:
                base = PRIM.get(idx & 0xFF)
                return (f"ds2::gptr<{base[0] if base and base[0] != 'void' else 'void'}>", "")
            p = PRIM.get(idx)
            if not p or p[1] == 0:
                return None
            return (p[0] if p[1] == 1 else f"ds2::be<{p[0]}>", "")
        r = self.m.recs[idx]
        k, b = r["kind"], " ".join(r["body"])
        if k == "LF_POINTER":
            if "member" in b:
                return None
            ref = ti(re.search(r"referent = (0x[0-9A-F]+)", b).group(1))
            u = self.udt_of(ref)
            if u is not None:
                return (f"ds2::gptr<{self.names[u]}>", "")
            refs = self.m.strip_mod(ref)
            if refs < 0x1000 and refs in PRIM and PRIM[refs][1]:
                return (f"ds2::gptr<{PRIM[refs][0]}>", "")
            return ("ds2::gptr<void>", "")
        if k == "LF_ARRAY":
            el = ti(re.search(r"element type: (0x[0-9A-F]+)", b).group(1))
            total = int(re.search(r"size: (\d+)", b).group(1))
            es = self.m.size_of(el)
            sub = self.cpp_type(el, deps)
            if not es or sub is None or total % es:
                return None
            return (sub[0], f"[{total // es}]" + sub[1])
        if k in ("LF_CLASS", "LF_STRUCTURE", "LF_UNION"):
            u = self.udt_of(idx)
            if u is None or not self.m.size_of(idx):
                return None
            deps.add(u)
            return (self.names[u], "")
        if k == "LF_ENUM":
            full = self.m.resolve_fwd(idx)
            sz = self.m.size_of(idx)
            if full in self.enum_names and sz:
                return (f"ds2::be<{self.enum_names[full]}>" if sz > 1 else self.enum_names[full], "")
            um = re.search(r"underlying type: (0x[0-9A-F]+)", self.m.body(full))
            return self.cpp_type(ti(um.group(1)), deps) if um else None
        return None

    def emit_enum(self, idx):
        r = self.m.recs[idx]
        b = " ".join(r["body"])
        u = self.m.strip_mod(ti(re.search(r"underlying type: (0x[0-9A-F]+)", b).group(1)))
        ut, usz = PRIM.get(u, ("int32_t", 4))
        fl = re.search(r"field list: (0x[0-9A-F]+)", b)
        items, seen = [], set()
        if fl:
            for kind, hdr, _ in field_list(self.m.recs[ti(fl.group(1))]["body"]):
                m = re.match(r"\[(.*) = (-?\d+)\]$", hdr)
                if kind == "LF_ENUMERATE" and m:
                    n = ident(m.group(1))
                    while n in seen:
                        n += "_"
                    seen.add(n)
                    v = int(m.group(2))
                    if ut.startswith("u"):
                        v &= (1 << (8 * usz)) - 1
                    else:
                        lim = 1 << (8 * usz - 1)
                        v = ((v + lim) % (2 * lim)) - lim
                    items.append(f"  {n} = {v},")
        return [f"// {r['name']}", f"enum class {self.enum_names[idx]} : {ut} {{", *items, "};", ""]

    def vtbl(self, name, fl_entries):
        slots = {}
        for kind, hdr, cont in fl_entries:
            if kind == "LF_ONEMETHOD":
                mn = re.search(r"name = `([^`]*)`", hdr).group(1)
                for c in cont:
                    m = re.search(r"vftable offset = (\d+), attrs = .*intro virtual", c)
                    if m:
                        slots.setdefault(int(m.group(1)), []).append(mn)
            elif kind == "LF_METHOD":
                m = re.search(r"name = `([^`]*)`, # overloads = \d+, overload list = (0x[0-9A-F]+)", hdr)
                if m:
                    for l in self.m.recs[ti(m.group(2))]["body"]:
                        mm = re.search(r"vftable offset = (\d+), attrs = .*intro virtual", l)
                        if mm:
                            slots.setdefault(int(mm.group(1)), []).append(m.group(1))
        if not slots:
            return []
        self.vtbls += 1
        lines = ["// vtable slots this class introduces (vftable offset / 4); entries are guest code addresses",
                 f"struct {name}_vtbl {{"]
        seen = set()
        for off in range(0, max(slots) + 4, 4):
            if off in slots:
                n = "v_" + re.sub(r"[^A-Za-z0-9_]", "_", slots[off][0]).strip("_")
                while n in seen or n in RESERVED:
                    n += "_"
                seen.add(n)
                lines.append(f"  ds2::be<uint32_t> {n};  // +0x{off:X} {' / '.join(slots[off])}")
            else:
                lines.append(f"  ds2::be<uint32_t> inherited_{off // 4};  // +0x{off:X} (a base class slot)")
        return lines + ["};", ""]

    def emit_udt(self, idx):
        r = self.m.recs[idx]
        b = " ".join(r["body"])
        size = int(re.search(r"sizeof (\d+)", b).group(1))
        name = self.names[idx]
        fl = re.search(r"field list: (0x[0-9A-F]+)", b)
        entries = field_list(self.m.recs[ti(fl.group(1))]["body"]) if fl else []
        fields, deps = [], set()  # [offset, spelling, field name, comment]
        bits: dict[int, list] = {}
        seen = set()

        def fname(n):
            n = ident(n) if n else "field"
            while n in seen:
                n += "_"
            seen.add(n)
            return n
        for kind, hdr, cont in entries:
            if kind == "LF_MEMBER":
                m = MEMBER.search(hdr)
                n, t, off = m.group(1), ti(m.group(2)), int(m.group(3))
                tr = self.m.recs.get(t, {})
                if tr.get("kind") == "LF_BITFIELD":
                    tb = " ".join(tr["body"])
                    bm = re.search(r"type = (0x[0-9A-F]+).*bit offset = (\d+), # bits = (\d+)", tb)
                    bits.setdefault(off, [ti(bm.group(1))]).append(f"{n}: bit offset {bm.group(2)}, {bm.group(3)} bits")
                    continue
                fields.append([off, self.cpp_type(t, deps), fname(n), ""])
            elif kind == "LF_BCLASS":
                m = re.search(r"type = (0x[0-9A-F]+), offset = (\d+)", " ".join(cont))
                t, off = ti(m.group(1)), int(m.group(2))
                u = self.udt_of(t)
                bn = self.names[u] if u is not None else f"ti{t:x}"
                fields.append([off, self.cpp_type(t, deps), fname("base_" + bn), "base class " + (self.m.recs[t]["name"] or "")])
            elif kind == "LF_VFUNCTAB":
                fields.append([0, ("ds2::gptr<void>", ""), fname("vfptr"), "vtable pointer"])
            elif kind == "LF_VBCLASS":
                self.m.refusals.append(f"{r['name']}: virtual base class (that base's layout not emitted)")
        for off, (ut, *notes) in sorted(bits.items()):
            self.bitfield_units += 1
            fields.append([off, self.cpp_type(ut, deps), fname(f"bits_{off}"), "bitfields: " + "; ".join(notes)])
        offs = sorted({f[0] for f in fields} | {size})
        lines = [f"// {r['name']}  (PDB type 0x{idx:X}, sizeof {size})",
                 f"{'union' if r['kind'] == 'LF_UNION' else 'struct'} {name} {{", "  union {"]
        tests = [f"static_assert(sizeof(ds2::pdb::{name}) == {size});"]
        for off, sp, fn, cm in fields:
            if off >= size:
                self.m.refusals.append(f"{r['name']}.{fn}: offset {off} >= sizeof {size}")
                continue
            nxt = next(o for o in offs if o > off)
            if sp is None:
                self.opaque_fields += 1
                cm = (cm + "; " if cm else "") + "opaque: type size unknown"
                spell = f"uint8_t {fn}[{nxt - off}];"
            else:
                spell = f"{sp[0]} {fn}{sp[1]};"
            self.emitted_fields += 1
            pad = f"uint8_t ds2pad{len(tests)}[{off}]; " if off else ""
            lines.append(f"    struct {{ {pad}{spell} }};" + (f"  // {cm}" if cm else ""))
            tests.append(f"static_assert(offsetof(ds2::pdb::{name}, {fn}) == {off});")
        lines += [f"    uint8_t ds2size[{size}];", "  };", "};", ""]
        return lines + self.vtbl(name, entries), tests, deps

    def run(self):
        enum_lines = []
        for idx in sorted(self.m.enums):
            enum_lines += self.emit_enum(idx)
        bodies, order, temp, done = {}, [], set(), set()
        for idx in sorted(self.m.udts):
            sz = re.search(r"sizeof (\d+)", self.m.body(idx))
            if not sz or int(sz.group(1)) == 0:
                self.m.refusals.append(f"{self.m.recs[idx]['name']}: sizeof 0")
                continue
            bodies[idx] = self.emit_udt(idx)
        sys.setrecursionlimit(100000)

        def visit(i):
            if i in done or i not in bodies:
                return
            if i in temp:
                raise Fail(f"by-value cycle through {self.names[i]}")
            temp.add(i)
            for d in sorted(bodies[i][2]):
                visit(d)
            temp.discard(i)
            done.add(i)
            order.append(i)
        for i in sorted(bodies):
            visit(i)
        fwd = [("union " if self.m.recs[i]["kind"] == "LF_UNION" else "struct ") + self.names[i] + ";" for i in sorted(bodies)]
        hdr = ["// Generated by tools/modapi/gen_types.py from the PDB TPI stream - do not edit, do not commit.",
               "#pragma once", "#include <cstddef>", "#include <cstdint>", '#include "ds2_be.h"', "",
               "#if defined(__clang__)", "#pragma clang diagnostic push",
               '#pragma clang diagnostic ignored "-Wgnu-anonymous-struct"',
               '#pragma clang diagnostic ignored "-Wnested-anon-types"', "#endif",
               "#pragma pack(push, 1)", "namespace ds2::pdb {", ""] + enum_lines + fwd + [""]
        test = ["// Generated by tools/modapi/gen_types.py - do not edit, do not commit.",
                '#include "ds2_types.h"', "#include <cstddef>",
                "#if defined(__clang__)", '#pragma clang diagnostic ignored "-Winvalid-offsetof"', "#endif"]
        nassert = 0
        for i in order:
            hdr += bodies[i][0]
            test += bodies[i][1]
            nassert += len(bodies[i][1])
        hdr += ["}  // namespace ds2::pdb", "#pragma pack(pop)", "#if defined(__clang__)",
                "#pragma clang diagnostic pop", "#endif", ""]
        test += ["int main() { return 0; }", ""]
        self.m.stats.update(emitted_types=len(order), emitted_enums=len(self.m.enums),
                            emitted_fields=self.emitted_fields, opaque_fields=self.opaque_fields,
                            bitfield_units=self.bitfield_units, vtables=self.vtbls, static_asserts=nassert)
        return "\n".join(hdr), "\n".join(test), order


def pretty_layout(pdb: Path, names: list[str]) -> dict[str, dict]:
    """Second reader (DIA): llvm-pdbutil pretty layout -> {name: {sizeof, members{name: offset}}}."""
    env = dict(os.environ)
    env["PATH"] = str(DIA_DIR) + os.pathsep + env.get("PATH", "")
    out = {}
    for chunk in range(0, len(names), 40):
        part = names[chunk:chunk + 40]
        rx = "^(" + "|".join(re.escape(n) for n in part) + ")$"
        p = subprocess.run([str(PDBUTIL), "pretty", "-classes", "-class-definitions=layout",
                            "-include-types=" + rx, str(pdb)], capture_output=True, text=True, errors="replace", env=env)
        if "NoRegCoCreate" in p.stderr + p.stdout:
            raise Fail("llvm-pdbutil pretty needs msdia140.dll (DIA) in " + str(DIA_DIR))
        cur = None
        for ln in p.stdout.splitlines():
            m = re.match(r"^    (?:class|struct|union) (.+?) \[sizeof = (\d+)\]", ln)
            if m:
                cur = out.setdefault(m.group(1), {"sizeof": int(m.group(2)), "members": {}})
                continue
            m = re.match(r"^      data \+0x([0-9a-f]+) \[sizeof=\d+\] .*?(\w+)((?:\[\d+\])*)\s*$", ln)
            if m and cur is not None:
                cur["members"].setdefault(m.group(2), int(m.group(1), 16))
    return out


def sample_check(pdb: Path, m: Model, order, n: int, seed=7):
    rng = random.Random(seed)
    cands = [i for i in order if not m.udts[i]["suffix"] and m.recs[i]["kind"] != "LF_UNION"
             and re.fullmatch(r"[A-Za-z0-9_:]+", m.udts[i]["name"])]
    pick = rng.sample(cands, min(n, len(cands)))
    got = pretty_layout(pdb, [m.udts[i]["name"] for i in pick])
    ok = bad = missing = mchecked = 0
    errs = []
    for i in pick:
        name = m.udts[i]["name"]
        size = int(re.search(r"sizeof (\d+)", m.body(i)).group(1))
        g = got.get(name)
        if not g:
            missing += 1
            continue
        good = g["sizeof"] == size
        if not good:
            errs.append(f"{name}: sizeof dump {size} vs pretty {g['sizeof']}")
        fl = re.search(r"field list: (0x[0-9A-F]+)", m.body(i))
        mine = {}
        for kind, hdr, _ in (field_list(m.recs[ti(fl.group(1))]["body"]) if fl else []):
            mm = MEMBER.search(hdr)
            if kind == "LF_MEMBER" and mm:
                mine.setdefault(mm.group(1), int(mm.group(3)))
        for k, off in g["members"].items():
            if k in mine:
                mchecked += 1
                if mine[k] != off:
                    good = False
                    errs.append(f"{name}.{k}: dump {mine[k]} vs pretty {off}")
        ok += good
        bad += not good
    return {"sampled": len(pick), "agree": ok, "disagree": bad, "not_found_by_pretty": missing,
            "member_offsets_compared": mchecked, "errors": errs[:20]}


def write_if_changed(p: Path, text: str) -> bool:
    if p.is_file() and p.read_text(encoding="utf-8") == text:
        return False
    p.write_text(text, encoding="utf-8", newline="\n")
    return True


def compile_test(out: Path) -> tuple[int, str]:
    p = subprocess.run([str(CLANG), "-std=c++23", "-fsyntax-only", "-Wall", f"-I{REPO / 'src' / 'modapi'}",
                        f"-I{out}", str(out / "ds2_types_test.cpp")], capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)[-4000:]


SELFTEST_DUMP = """\
    0x1000 | LF_FIELDLIST [size = 40]
             - LF_ENUMERATE [A = 0]
             - LF_ENUMERATE [INFINITY = 7]
    0x1001 | LF_ENUM [size = 40] `T::E`
             field list: 0x1000, underlying type: 0x0074 (int)
             options: has unique name
    0x1002 | LF_FIELDLIST [size = 80]
             - LF_VFUNCTAB type = 0x1009
             - LF_MEMBER [name = `a`, Type = 0x0074 (int), offset = 4, attrs = public]
             - LF_MEMBER [name = `u1`, Type = 0x0040 (float), offset = 8, attrs = public]
             - LF_MEMBER [name = `u2`, Type = 0x0075 (unsigned), offset = 8, attrs = public]
             - LF_MEMBER [name = `e`, Type = 0x1001, offset = 12, attrs = public]
             - LF_MEMBER [name = `namespace`, Type = 0x0403 (void*), offset = 16, attrs = public]
             - LF_MEMBER [name = `arr`, Type = 0x1004, offset = 20, attrs = public]
             - LF_ONEMETHOD [name = `F`]
               type = 0x1010, vftable offset = 0, attrs = public intro virtual
    0x1003 | LF_STRUCTURE [size = 60] `T::S`
             unique name: `US@T@@`
             vtable: 0x1009, base list: <no type>, field list: 0x1002
             options: has unique name, sizeof 32
    0x1004 | LF_ARRAY [size = 16]
             size: 12, index type: 0x0022 (unsigned long), element type: 0x0021 (unsigned short)
"""


def selftest() -> int:
    bad = 0
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        m = Model(parse_dump(SELFTEST_DUMP.splitlines()))
        m.collect(None)
        h, t, order = Emitter(m).run()
        (out / "ds2_types.h").write_text(h, encoding="utf-8")
        (out / "ds2_types_test.cpp").write_text(t, encoding="utf-8")
        rc, log = compile_test(out)
        checks = [("synthetic struct emitted", "struct T__S {" in h),
                  ("overlapping members at one offset", "u1;" in h and "u2;" in h and "offsetof(ds2::pdb::T__S, u2) == 8" in t),
                  ("enum emitted with underlying type", "enum class T__E : int32_t" in h),
                  ("macro name renamed (INFINITY -> INFINITY_)", "INFINITY_ = 7" in h),
                  ("keyword renamed (namespace -> namespace_)", "namespace_;" in h),
                  ("array of be<u16>[6]", "ds2::be<uint16_t> arr[6]" in h),
                  ("vtable struct", "struct T__S_vtbl" in h),
                  ("positive: generated asserts compile", rc == 0)]
        (out / "ds2_types_test.cpp").write_text(t.replace("offsetof(ds2::pdb::T__S, a) == 4", "offsetof(ds2::pdb::T__S, a) == 5"), encoding="utf-8")
        rc2, log2 = compile_test(out)
        checks.append(("negative: a perturbed offset assert fails to compile", rc2 != 0 and "static assertion failed" in log2))
        (out / "ds2_types_test.cpp").write_text(t.replace("sizeof(ds2::pdb::T__S) == 32", "sizeof(ds2::pdb::T__S) == 36"), encoding="utf-8")
        rc3, log3 = compile_test(out)
        checks.append(("negative: a perturbed sizeof assert fails to compile", rc3 != 0 and "static assertion failed" in log3))
        for label, ok in checks:
            print(("PASS " if ok else "FAIL ") + label)
            bad += not ok
        if rc != 0:
            print(log)
    print(("SELFTEST_OK" if not bad else "SELFTEST_FAIL") + f" {len(checks) - bad}/{len(checks)}")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdb")
    ap.add_argument("--out")
    ap.add_argument("--include", default=None, help="regex on the PDB type name")
    ap.add_argument("--compile-test", action="store_true")
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    try:
        nmac = load_reserved()
    except Fail as e:
        print("GEN_TYPES_FAIL", e)
        return 1
    print(f"macro probe: {nmac} macro names reserved")
    if a.selftest:
        return selftest()
    if not (a.pdb and a.out):
        ap.error("--pdb and --out are required")
    pdb, out = Path(a.pdb), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    p = subprocess.run([str(PDBUTIL), "dump", "--types", str(pdb)], capture_output=True, text=True, errors="replace")
    if p.returncode != 0 or "LF_FIELDLIST" not in p.stdout:
        print("GEN_TYPES_FAIL llvm-pdbutil dump --types failed:", p.stderr[-500:])
        return 1
    pdb_sha = hashlib.sha256(pdb.read_bytes()).hexdigest()
    try:
        m = Model(parse_dump(p.stdout.splitlines()))
        m.collect(a.include)
        h, t, order = Emitter(m).run()
    except Fail as e:
        print("GEN_TYPES_FAIL", e)
        return 1
    written = sum([write_if_changed(out / "ds2_types.h", h), write_if_changed(out / "ds2_types_test.cpp", t)])
    receipt = {"pdb": str(pdb), "pdb_sha256": pdb_sha, "type_records": len(m.recs), **m.stats,
               "refusals": len(m.refusals), "refusal_examples": m.refusals[:30],
               "header_sha256": hashlib.sha256(h.encode()).hexdigest()}
    ok = True
    if a.sample:
        try:
            sc = sample_check(pdb, m, order, a.sample)
        except Fail as e:
            print("GEN_TYPES_FAIL", e)
            return 1
        receipt["sample_cross_check"] = sc
        ok &= sc["disagree"] == 0 and sc["agree"] > 0 and sc["member_offsets_compared"] > 0
        print(f"sample cross-check vs llvm-pdbutil pretty (DIA): {sc['agree']}/{sc['sampled']} types agree, "
              f"{sc['disagree']} disagree, {sc['not_found_by_pretty']} not found; {sc['member_offsets_compared']} member offsets compared")
        for e in sc["errors"]:
            print("  ", e)
    if a.compile_test:
        rc, log = compile_test(out)
        receipt["compile_test"] = {"rc": rc, "static_asserts": m.stats["static_asserts"]}
        print(f"compile test: rc={rc}, {m.stats['static_asserts']} static_asserts")
        if rc != 0:
            print(log)
        ok &= rc == 0
    written += write_if_changed(out / "ds2_types.json", json.dumps(receipt, indent=1) + "\n")
    s = m.stats
    print(f"types: {s['emitted_types']} emitted ({s['names']} names with a full definition, {s['full_definitions']} definitions; "
          f"{s['names_with_different_definitions']} names with different definitions -> __ti suffix); "
          f"{s['emitted_enums']} enums, {s['vtables']} vtables, {s['emitted_fields']} fields ({s['opaque_fields']} opaque, "
          f"{s['bitfield_units']} bitfield units); {len(m.refusals)} refusals")
    print(("GEN_TYPES_OK" if ok else "GEN_TYPES_FAIL") + f" {written} files written, {3 - written} unchanged -> {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
