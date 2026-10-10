"""PDB reader adapter.

The MSF / DBI / CodeView parsing is delegated to an external reader module (default:
xb360_toolkit/lanes/identity_donor.py, the reader all DS2 measurements used; configure
`[pdb] reader_dir`). This module turns it into the four tables the chain consumes:

  procs     proto_va size name module_index lib obj src_file src_line calltype nparams has_this this_adjust
  contribs  proto_va size module_index lib obj    (code section contributions, link order)
  dcontribs same, initialised-data contributions
  thunks    proto_va size name module_index lib obj ordinal   (S_THUNK32)

Column naming follows the DS2 tables: `obj` is the compiland (DBI module name), `lib` the
library/object file string of the module record. Any parse failure is a refusal.
"""
from __future__ import annotations

import bisect, importlib, struct, sys

CALLTYPES = {0x00: "near_c", 0x04: "near_fast", 0x07: "near_std", 0x0B: "thiscall",
             0x0D: "mipscall", 0x0E: "generic", 0x0F: "alphacall", 0x10: "ppccall",
             0x11: "shcall", 0x12: "armcall", 0x18: "clrcall", 0x19: "inline", 0x1A: "near_vector"}
PROC_HEADER = ["proto_va", "size", "name", "module_index", "lib", "obj", "src_file", "src_line",
               "calltype", "nparams", "has_this", "this_adjust"]
CONTRIB_HEADER = ["proto_va", "size", "module_index", "lib", "obj"]
THUNK_HEADER = ["proto_va", "size", "name", "module_index", "lib", "obj", "ordinal"]
PUBLIC_HEADER = ["proto_va", "name", "flags"]


def load_reader(reader_dir: str):
    if reader_dir not in sys.path:
        sys.path.insert(0, reader_dir)
    D = importlib.import_module("identity_donor")
    PEImage = importlib.import_module("identity_image").PEImage
    return D, PEImage


def _names_stream(D, msf) -> bytes:
    info = msf.stream(1)
    r = D.Reader(info, 28)
    buf = r.take(r.u32())
    _size, cap = r.u32(), r.u32()
    present = [r.u32() for _ in range(r.u32())]
    _deleted = [r.u32() for _ in range(r.u32())]
    found = None
    for i in range(cap):
        if present[i // 32] >> (i % 32) & 1 if i // 32 < len(present) else 0:
            key, val = r.u32(), r.u32()
            end = buf.find(b"\0", key)
            if buf[key:end] == b"/names":
                found = val
    if found is None:
        raise D.DonorError("no /names stream")
    s = msf.stream(found)
    magic, _ver, blen = struct.unpack_from("<III", s, 0)
    if magic != 0xEFFEEFFE:
        raise D.DonorError("bad /names magic")
    return s[12:12 + blen]


def _cstr(buf, off):
    return buf[off:buf.find(b"\0", off)].decode("utf-8", "replace")


def _type_table(D, data):
    _ver, hsize, ti, ti_end = struct.unpack_from("<IIII", data, 0)
    out = {}
    for _off, kind, body, _raw in D.records(data, hsize):
        out[ti] = (kind, body)
        ti += 1
    if ti != ti_end:
        raise D.DonorError("type stream index count mismatch")
    return out


def _proc_type(tpi, ipi, ti, is_id):
    if is_id:
        rec = ipi.get(ti)
        if not rec or rec[0] not in (0x1601, 0x1602):
            return None
        ti = struct.unpack_from("<II", rec[1], 0)[1]
    rec = tpi.get(ti)
    if not rec:
        return None
    kind, body = rec
    if kind == 0x1008:
        _rv, call, _attr, n, _al = struct.unpack_from("<IBBHI", body, 0)
        return call, n, False, 0
    if kind == 0x1009:
        _rv, _cls, this, call, _attr, n, _al, adj = struct.unpack_from("<IIIBBHIi", body, 0)
        return call, n, this != 0, adj
    return None


def _c13_lines(data, start, length, names):
    p, end = start, start + length
    checks, subs = {}, []
    while p + 8 <= end:
        kind, size = struct.unpack_from("<II", data, p)
        body = data[p + 8:p + 8 + size]
        if kind == 0xF4:
            q = 0
            while q + 6 <= len(body):
                name_off, cb, _t = struct.unpack_from("<IBB", body, q)
                checks[q] = _cstr(names, name_off)
                q += 6 + cb
                q += (-q) % 4
        elif kind == 0xF2:
            subs.append(body)
        p += 8 + size
        p += (-p) % 4
    out = []
    for body in subs:
        off_con, seg, _flags, cb = struct.unpack_from("<IHHI", body, 0)
        q, ent = 12, []
        while q + 12 <= len(body):
            fid, n, cbb = struct.unpack_from("<III", body, q)
            for k in range(n):
                o, ln = struct.unpack_from("<II", body, q + 12 + 8 * k)
                ent.append((off_con + o, fid, ln & 0xFFFFFF))
            q += cbb
        ent.sort()
        out.append((seg, off_con, off_con + cb, [(o, checks.get(f, "?"), ln) for o, f, ln in ent]))
    return out


def extract_tables(reader_dir: str, pe: str, pdb: str):
    """-> dict(procs, contribs, dcontribs, thunks, stats, pdb_sha256); rows are string lists."""
    D, PEImage = load_reader(reader_dir)
    image, msf = PEImage(pe), D.MSF(pdb)
    D.validate_identity(image, msf)
    dbi, modules = D.parse_dbi(msf)
    secs = {s["index"]: s for s in image.sections}
    names = _names_stream(D, msf)
    tpi = _type_table(D, msf.stream(2))
    try:
        ipi = _type_table(D, msf.stream(4, optional=True))
    except Exception:
        ipi = {}
    rows, thunks = [], []
    stats = {"modules": len(modules), "procs": 0, "with_line": 0, "with_type": 0}
    for m in modules:
        if m["stream"] == 0xFFFF:
            continue
        data = msf.stream(m["stream"])
        sb, c11, c13 = m["symbol_bytes"], m["c11_bytes"], m["c13_bytes"]
        subs = _c13_lines(data, sb + c11, c13, names) if c13 else []
        for _off, kind, body, _raw in D.records(data[:sb], 4):
            if kind == 0x1102:
                s = D.decode_symbol(kind, body)
                sec = secs.get(s["section_index"])
                if sec and s.get("size"):
                    thunks.append(["0x%08X" % (image.base + sec["rva"] + s["offset"]), str(s["size"]), s["name"],
                                   str(m["index"]), m["object"], m["module"], str(s.get("ordinal", ""))])
                continue
            if kind not in (0x110F, 0x1110, 0x1146, 0x1147):
                continue
            s = D.decode_symbol(kind, body)
            sec = secs.get(s["section_index"])
            if not sec:
                continue
            va = image.base + sec["rva"] + s["offset"]
            stats["procs"] += 1
            src = line = ""
            for seg, b, e, ent in subs:
                if seg == s["section_index"] and b <= s["offset"] < e and ent:
                    i = bisect.bisect_right(ent, (s["offset"], "￿", 1 << 30)) - 1
                    if i >= 0:
                        src, line = ent[i][1], str(ent[i][2])
                        break
            if src:
                stats["with_line"] += 1
            t = _proc_type(tpi, ipi, int(s["type_index"], 16), kind in (0x1146, 0x1147))
            if t:
                stats["with_type"] += 1
                call, n, this, adj = t
                tcols = [CALLTYPES.get(call, "0x%02X" % call), str(n), "1" if this else "0", str(adj)]
            else:
                tcols = ["", "", "", ""]
            rows.append(["0x%08X" % va, str(s["size"]), s["name"], str(m["index"]), m["object"],
                         m["module"], src, line] + tcols)
    rows.sort(key=lambda r: (int(r[0], 16), r[2], r[3]))
    craw = bytes.fromhex(dbi["substreams"]["section_contributions"]["raw_hex"])
    contribs, _ = D.parse_contributions(craw, image.sections, image.base)
    crow, drow = [], []
    for c in contribs:
        if not c.get("va") or c["size"] <= 0 or not c["characteristics"] & 0x60:
            continue
        m = modules[c["module_index_0based"]]
        r = ["0x%08X" % int(c["va"], 16), str(c["size"]), str(m["index"]), m["object"], m["module"]]
        if c["characteristics"] & 0x20:
            crow.append(r)
        elif c["characteristics"] & 0x40:
            drow.append(r)
    crow.sort(key=lambda r: int(r[0], 16))
    drow.sort(key=lambda r: int(r[0], 16))
    thunks.sort(key=lambda r: int(r[0], 16))
    # S_PUB32 (globals symbol stream): mangled public names, incl. thunks/adjustors without a proc
    publics = []
    if dbi.get("symbol_stream", 0xFFFF) != 0xFFFF:
        for _off, kind, body, _raw in D.records(msf.stream(dbi["symbol_stream"]), 0):
            if kind == 0x110E:
                flags, off, seg = struct.unpack_from("<IIH", body, 0)
                sec = secs.get(seg)
                if sec:
                    nm = body[10:body.find(bytes(1), 10)].decode("latin1")
                    publics.append(["0x%08X" % (image.base + sec["rva"] + off), nm, str(flags)])
    publics.sort(key=lambda r: (int(r[0], 16), r[1]))
    if not rows or stats["with_line"] == 0:
        raise D.DonorError("no procedures or no line info")
    stats.update(code_contributions=len(crow), data_contributions=len(drow), thunks=len(thunks), publics=len(publics))
    return {"procs": rows, "publics": publics, "contribs": crow, "dcontribs": drow, "thunks": thunks, "stats": stats,
            "pdb_sha256": msf.sha256}


def extract_donor(reader_dir: str, pe: str, pdb: str):
    """Full symbol/contribution extraction (used by the evidence map)."""
    D, _ = load_reader(reader_dir)
    return D.extract_donor(str(pe), str(pdb)), D
