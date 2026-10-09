r"""compare_objs_exact.py -- byte-exact A/B of the codegen TU objects: flat build vs named build.

    python tools/diagnostics/compare_objs_exact.py --a <flat obj dir> --b <named _units obj dir>

The named build compiles app/generated_named/_units/<project>_unit.N.cpp, which resolves to
codegen TU N function for function. So object N of each build is compared WHOLE, with no
heuristics: every section's raw bytes (.text, .rdata incl. jump tables and constant pools,
.pdata, .xdata, ...), every relocation (offset, type, symbol) and the symbol table (name,
value, section) must be equal. Only .debug$* sections are skipped: they name the source
files and lines, which is the point of the named tree.

Controls, every run, on copies (never the build's own files):
  negative  object A compared with a byte copy of itself -> must be EQUAL
  positive  the same copy with one .text byte flipped    -> must be DIFFERENT
Exit 0 and EXACT PASS only when all pairs are equal and both controls behave.
"""
from __future__ import annotations

import argparse, concurrent.futures as cf, hashlib, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

READOBJ = r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\Llvm\x64\bin\llvm-readobj.exe"


# A C identifier bounded by non-identifier characters: also finds the function name inside the
# symbols clang derives from it ($cppxdata$__imp__X, ?dtor$13@?0?__imp__X@4HA, $ip2state$...).
WORD = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*")
# A section name over 8 bytes is stored as "/<string table offset>"; readobj prints those raw
# bytes after the name. The offset moves when symbol names change length -- the name is kept.
SECNAME_RAW = re.compile(r"^(Name: \S+) \((?:[0-9A-F]{2} ){7}[0-9A-F]{2}\)$")


def load_name_map(path: Path) -> dict:
    """symbols.tsv (organize_generated.py --ids pdb): renamed symbol -> codegen symbol, plus the
    __imp__ forms DEFINE_REX_FUNC pastes. Refuses a non-injective map."""
    m = {}
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        _a, old, new = line.split("\t")
        if old != new:
            if new in m:
                raise SystemExit("name map: %s listed twice" % new)
            m[new] = old
            m["__imp__" + new] = "__imp__" + old
    return m


HEXDUMP = re.compile(r"^[0-9A-F]{4,}: ")
NAME_MODE = False


def normalise(line: str, nmap: dict) -> str:
    """Symbol names only (symbol table, relocation targets): raw section bytes are never touched."""
    if not NAME_MODE or HEXDUMP.match(line):  # default mode: byte-for-byte the previous digest
        return line
    m = SECNAME_RAW.match(line)  # both sides, in --name-map mode
    if m:
        line = m.group(1)
    if not nmap:
        return line
    return WORD.sub(lambda w: nmap.get(w.group(0), w.group(0)), line) if nmap else line


def digest(obj: Path, nmap: dict | None = None) -> dict:
    p = subprocess.run([READOBJ, "--sections", "--section-data", "--relocations", "--symbols", str(obj)],
                       capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        raise SystemExit("readobj failed: %s" % obj)
    out, skip, cur = {}, False, None
    sec_names = {}
    for line in p.stdout.splitlines():
        s = line.strip()
        m = re.match(r"^Number: (\d+)$", s)
        if m:
            cur = int(m.group(1))
        m = re.match(r"^Name: (\S+)", s)
        if m and cur is not None and cur not in sec_names:
            sec_names[cur] = m.group(1)
    h = hashlib.sha256()
    # second pass: hash everything except debug sections, file names and readobj's own paths
    section = None
    in_symbols = False
    skip_sym = False
    for line in p.stdout.splitlines():
        s = line.strip()
        if s.startswith("File: "):
            continue
        if s == "Symbols [":
            in_symbols = True
        m = re.match(r"^Number: (\d+)$", s)
        if m and not in_symbols:
            section = sec_names.get(int(m.group(1)), "")
        m = re.match(r"^Section \((\d+)\) (\S+) \{$", s)  # relocation group header
        if m:
            section = m.group(2)
        if section and section.startswith(".debug$"):
            continue
        if in_symbols:
            if s == "Symbol {":
                skip_sym = False
            if s.startswith("Name: .debug$") or s.startswith("Name: .file"):
                skip_sym = True  # the section symbol of a debug section, with its aux length/checksum
            if skip_sym:
                continue
        if s.startswith("PointerToRawData") or s.startswith("PointerToRelocations") or s.startswith("PointerToLineNumbers"):
            continue  # file offsets move when the (skipped) debug sections change size
        h.update(normalise(s, nmap).encode() + b"\n")
    return {"sha": h.hexdigest()}


def unit_key(p: Path) -> int:
    return int(re.search(r"\.(\d+)\.cpp\.obj$", p.name).group(1))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 8)
    ap.add_argument("--a-glob", default="*_recomp.*.cpp.obj",
                    help="object pattern in --a (default: a flat build; *_unit.*.cpp.obj compares two named builds)")
    ap.add_argument("--b-glob", default="*_unit.*.cpp.obj",
                    help="object pattern in --b (default: the named tree's units; "
                         "*_recomp.*.cpp.obj compares two flat builds, e.g. mods off vs on)")
    ap.add_argument("--name-map", default=None,
                    help="symbols.tsv of an organize --ids pdb tree: B's symbol names (symbol table and "
                         "relocation targets) are mapped back to codegen's before comparing; bytes untouched")
    ap.add_argument("--support-a", default=None, help="dir holding A's *_init.cpp.obj / *_register.cpp.obj")
    ap.add_argument("--support-b", default=None, help="dir holding B's *_init.cpp.obj / *_register.cpp.obj")
    a = ap.parse_args(argv)
    nmap = load_name_map(Path(a.name_map)) if a.name_map else {}
    global NAME_MODE
    NAME_MODE = bool(nmap)
    A = {unit_key(p): p for p in Path(a.a).glob(a.a_glob)}
    B = {unit_key(p): p for p in Path(a.b).glob(a.b_glob)}
    if a.support_a and a.support_b:
        for k, pat in (("init", "*_init.cpp.obj"), ("register", "*_register.cpp.obj")):
            pa, pb = sorted(Path(a.support_a).glob(pat)), sorted(Path(a.support_b).glob(pat))
            if len(pa) != 1 or len(pb) != 1:
                raise SystemExit("support object %s: %d in A, %d in B (need exactly 1 each)" % (pat, len(pa), len(pb)))
            A[k], B[k] = pa[0], pb[0]
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        da = dict(zip(A, ex.map(digest, A.values())))
        db = dict(zip(B, ex.map(lambda p: digest(p, nmap), B.values())))
    keyf = lambda k: (isinstance(k, str), k if isinstance(k, int) else 0, str(k))  # noqa: E731
    same = sorted((k for k in A if k in B and da[k] == db[k]), key=keyf)
    diff = sorted((k for k in A if k in B and da[k] != db[k]), key=keyf)
    name_ctl = {}
    if nmap:
        # the map must be doing the work (raw names differ), and a WRONG map must be caught:
        # two renamed symbols swapped in the map -> the pair that uses both becomes different
        k0 = next(k for k in sorted(B, key=keyf) if da.get(k) == db.get(k))
        raw = digest(B[k0]) != da[k0]
        txt = subprocess.run([READOBJ, "--symbols", str(B[k0])], capture_output=True, text=True).stdout
        used = [n for n in re.findall(r"Name: (\S+)", txt) if n in nmap and not n.startswith("__imp__")]
        x, y = used[0], used[1]
        bad = dict(nmap)
        bad[x], bad[y] = nmap[y], nmap[x]
        bad["__imp__" + x], bad["__imp__" + y] = nmap["__imp__" + y], nmap["__imp__" + x]
        name_ctl = {"pair": k0, "raw_names_differ": raw, "swapped_map_detected": digest(B[k0], bad) != da[k0],
                    "swapped": [x, y]}
    # controls on copies
    tmp = Path(tempfile.mkdtemp())
    try:
        kmin = min(k for k in A if isinstance(k, int))
        src = A[kmin]
        c1 = tmp / src.name
        shutil.copyfile(src, c1)
        neg = digest(c1) == da[kmin]
        data = bytearray(c1.read_bytes())
        # flip one byte inside the first .text section's raw data
        p = subprocess.run([READOBJ, "--sections", str(c1)], capture_output=True, text=True).stdout
        m = re.search(r"Name: \.text .*?RawDataSize: (\d+)\s+PointerToRawData: 0x([0-9A-F]+)", p, re.S)
        off = int(m.group(2), 16) + int(m.group(1)) // 2
        data[off] ^= 0xFF
        c1.write_bytes(bytes(data))
        pos = digest(c1) != da[kmin]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("objects_a", len(A), "objects_b", len(B), "paired", len(set(A) & set(B)))
    print("identical", len(same), "different", len(diff), "first_different", diff[:10])
    print("control_negative_equal", neg, "control_positive_detected", pos)
    ok = neg and pos and not diff and len(same) == len(A) == len(B)
    if nmap:
        print("name_map", len(nmap) // 2, "control", name_ctl)
        ok = ok and name_ctl["raw_names_differ"] and name_ctl["swapped_map_detected"]
    print("EXACT PASS" if ok else "EXACT FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
