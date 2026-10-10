"""Stage E1: PDB evidence map, prototype -> retail (DS2 R1-13 v1 method, title-agnostic port).

Two independent classes per PDB symbol:
  bytes_class  EXACT / DELTA / REFUSED: each section contribution (one obj's chunk, in link
               order) gets one retail offset voted by the anchors inside it plus bl/lis+D operand
               pairs of byte-aligned anchors; EXACT when the bytes match at that offset (code:
               relocation mask; data: pointer words must move by their target's delta).
  name_class   NAMED:<kinds> when >= 2 independent kinds (pos, call, ref, shape) agree on one
               retail address and no other candidate exists; ANCHOR for the input anchors.
Gates (DS2 measured): byte LOO 0.99993 (14,271/14,272), name LOO 14,229/14,229; shift +4 admits 4
EXACT vs 38,391; shuffled anchor pairing 25 contributions / 20 names.

Anchors: a TSV with `name` and `va` columns (retail functions already identified by digest);
an anchor pairs with a PDB procedure only when the name is unique on both sides.
Output columns: proto_addr retail_addr size kind name contribution bytes_class name_class.
"""
from __future__ import annotations

import bisect, collections, csv, random, re

from ..arch import ppc
from ..readers import pdb as PDB
from ..readers.pe import PEImage, reader
from ..util import sha256_bytes, table_text, tsv

PROC = ("global_procedure", "local_procedure")
KEEP = PROC + ("thunk", "public", "global_data", "local_data", "global_thread_data", "local_thread_data")
HEADER = ["proto_addr", "retail_addr", "size", "kind", "name", "contribution", "bytes_class", "name_class"]


class Model:
    def __init__(s, conts, prd, rrd, n_delta, exact_min, lo, hi):
        s.conts, s.prd, s.rrd = conts, prd, rrd
        s.starts = [c["va"] for c in conts]
        s.bytecache, s.pdelta = {}, {}
        s.n_delta, s.exact_min, s.lo, s.hi = n_delta, exact_min, lo, hi

    def set_pass1(s, cls):
        s.pdelta = {ci: c[1] for ci, c in cls.items() if c[0] != "REFUSED" and s.conts[ci]["code"]}
        s.bytecache = {}

    def ptr_delta(s, u):
        ci = s.find(u)
        return s.pdelta.get(ci) if ci is not None else None

    def find(s, va):
        i = bisect.bisect_right(s.starts, va) - 1
        for j in (i, i - 1):
            if j >= 0 and s.conts[j]["va"] <= va < s.conts[j]["va"] + s.conts[j]["size"]:
                return j
        return None

    def bytes_ok(s, ci, delta):
        key = (ci, delta)
        if key not in s.bytecache:
            c = s.conts[ci]
            ok = False
            if c["size"] >= 4:
                a, b = s.prd(c["va"], c["size"]), s.rrd(c["va"] + delta, c["size"])
                if a is not None and b is not None and any(a):
                    ok = ppc.code_equal(a, b) if c["code"] else ppc.data_equal(a, b, s.ptr_delta, s.lo, s.hi)
            s.bytecache[key] = ok
        return s.bytecache[key]

    def classify(s, ci, votes, shift=0):
        by = collections.defaultdict(set)
        for d, src, _t, _k in votes:
            by[d].add(src)
        if not by:
            return "REFUSED", None, 0
        if len(by) > 1:
            return "REFUSED", None, -len(by)
        d, srcs = next(iter(by.items()))
        d += shift
        n = len(srcs)
        if s.bytes_ok(ci, d) and (n >= s.n_delta or s.conts[ci]["size"] >= s.exact_min):
            return "EXACT", d, n
        if n >= s.n_delta and shift == 0:
            return "DELTA", d, n
        return "REFUSED", None, n


def build_votes(model, pairs, prd, rrd, psize):
    votes = collections.defaultdict(list)
    tgt = collections.defaultdict(lambda: collections.defaultdict(set))
    aligned = 0
    for src, pa, ra in pairs:
        ci = model.find(pa)
        if ci is not None:
            votes[ci].append((ra - pa, src, pa, "anchor"))
        n = psize.get(pa)
        if not n:
            continue
        ops = ppc.operand_pairs(prd(pa, n), rrd(ra, n), pa, ra)
        if ops is None:
            continue
        aligned += 1
        for kind, pt, rt in ops:
            if not (model.lo <= pt < model.hi and model.lo <= rt < model.hi):
                continue
            cj = model.find(pt)
            if cj is None:
                continue
            votes[cj].append((rt - pt, src, pt, kind))
            tgt[pt][kind].add((rt, src))
    return votes, tgt, aligned


def symkind(row, sec):
    n, k = row["name"], row["kind"]
    if n.startswith("__imp_"):
        return "import"
    if n.startswith("??_7") or "`vftable'" in n:
        return "vtable"
    if n.startswith("??_R") or "`RTTI" in n:
        return "rtti"
    if n.startswith("??_C@") or n.startswith("`string'"):
        return "string"
    if n.startswith("$"):
        return "table"
    if k in PROC:
        return "proc"
    if k == "thunk":
        return "thunk"
    if k == "public":
        return "public_code" if sec == ".text" else "public_data"
    return "data"


def name_evidence(model, votes, cls, tgt, v, ppd, rpd, drop_src=None):
    cand = collections.defaultdict(set)
    ci = model.find(v)
    if ci is not None and ci in votes:
        vs = [x for x in votes[ci] if x[2] != v and x[1] != drop_src]
        c, d, _ = cls[ci] if len(vs) == len(votes[ci]) else model.classify(ci, vs)
        if c != "REFUSED":
            cand[v + d].add("pos")
    for kind in ("call", "ref"):
        for rt, src in tgt.get(v, {}).get(kind, ()):
            if src != drop_src:
                cand[rt].add(kind)
    if not cand:
        return ("REFUSED_INSUFFICIENT", None)
    if len(cand) > 1:
        return ("REFUSED_CONFLICT", None)
    r, kinds = next(iter(cand.items()))
    if v in ppd and r in rpd and ppd[v] == rpd[r]:
        kinds = kinds | {"shape"}
    if len(kinds) >= 2:
        return ("NAMED:" + "+".join(sorted(kinds)), r)
    return ("REFUSED_INSUFFICIENT", None)


def run_all(model, pairs, prd, rrd, psize, syms, ppd, rpd, anchor_of, hold_src=None):
    votes, tgt, aligned = build_votes(model, pairs, prd, rrd, psize)
    model.pdelta, model.bytecache = {}, {}
    model.set_pass1({ci: model.classify(ci, v) for ci, v in votes.items() if model.conts[ci]["code"]})
    cls = {ci: model.classify(ci, v) for ci, v in votes.items()}
    names = {}
    for sy in syms:
        v = sy["va"]
        if v in anchor_of and hold_src is None:
            names[sy["i"]] = ("ANCHOR", anchor_of[v])
            continue
        names[sy["i"]] = name_evidence(model, votes, cls, tgt, v, ppd, rpd)
    return votes, cls, names, tgt, aligned


def run(cfg, ctx):
    """cfg: evidence section + inputs; ctx: chain context (engine, writer, work dir, log)."""
    E = cfg["evidence"]
    lo, hi = int(E.get("code_window", ["0x82000000", "0x83000000"])[0], 16), int(E.get("code_window", ["0x82000000", "0x83000000"])[1], 16)
    prim = ctx.primary
    pim, rim = PEImage(prim["pe"]), PEImage(cfg["inputs"]["retail_pe"])
    prd, rrd = reader(pim), reader(rim)
    donor, _D = PDB.extract_donor(ctx.pdb_reader_dir, prim["pe"], prim["pdb"])
    secname = {s["index"]: s["name"] for s in pim.sections}
    mods = {m["index"]: m for m in donor["modules"]}
    conts = []
    for c in donor["section_contributions"]:
        if c.get("va") is None or c["size"] <= 0:
            continue
        m = mods.get(c["module_index_0based"], {})
        conts.append({"va": int(c["va"], 16), "size": c["size"], "sec": secname.get(c["section_index"], "?"),
                      "code": bool(c["characteristics"] & 0x20), "mod": m.get("module", "?"), "obj": m.get("object", "")})
    conts.sort(key=lambda c: (c["va"], c["size"], c["mod"]))
    model = Model(conts, prd, rrd, int(E.get("n_delta", 2)), int(E.get("exact_min", 16)), lo, hi)

    syms, psize, procnames = [], {}, collections.defaultdict(set)
    for s in donor["symbols"]:
        if s["kind"] not in KEEP or not s.get("va") or not s.get("name"):
            continue
        va = int(s["va"], 16)
        syms.append({"i": len(syms), "va": va, "name": s["name"], "kind": s["kind"], "size": s.get("size"),
                     "sec": s.get("section", "")})
        if s["kind"] in PROC:
            procnames[s["name"]].add(va)
            if s.get("size"):
                psize[va] = s["size"]
    for row in syms:
        row["k"] = symkind(row, row["sec"])
    syms.sort(key=lambda r: (r["va"], r["k"], r["name"]))
    for i, r in enumerate(syms):
        r["i"] = i

    rows = list(csv.DictReader(open(cfg["inputs"]["anchors"], encoding="utf-8"), delimiter="\t"))
    rn = collections.Counter(r["name"] for r in rows)
    rv = collections.Counter(r["va"].lower() for r in rows)
    pairs, seenp = [], collections.Counter()
    for r in rows:
        p = procnames.get(r["name"])
        if rn[r["name"]] == 1 and rv[r["va"].lower()] == 1 and p and len(p) == 1:
            pairs.append([next(iter(p)), int(r["va"], 16), r["name"]])
            seenp[pairs[-1][0]] += 1
    pairs = [x for x in pairs if seenp[x[0]] == 1]
    pairs.sort()
    apairs = [(i, pa, ra) for i, (pa, ra, _n) in enumerate(pairs)]
    anchor_of = {pa: ra for pa, ra, _n in pairs}
    ppd = {p["va"]: (p["size"], p["prolog"]) for p in pim.pdata()}
    rpd = {p["va"]: (p["size"], p["prolog"]) for p in rim.pdata()}
    ctx.log("evidence: %d symbols, %d contributions, %d anchor pairs" % (len(syms), len(conts), len(apairs)))

    votes, cls, names, tgt, aligned = run_all(model, apairs, prd, rrd, psize, syms, ppd, rpd, anchor_of)

    bytes_items, refused = [], 0
    for src, pa, ra in apairs:
        ci = model.find(pa)
        if ci is None:
            refused += 1
            continue
        c, d, _ = model.classify(ci, [x for x in votes[ci] if x[1] != src])
        if c == "REFUSED":
            refused += 1
        else:
            bytes_items.append((ra, pa + d == ra))
    name_items, nref = [], 0
    for src, pa, ra in apairs:
        ci = model.find(pa)
        if ci is None or cls.get(ci, ("REFUSED",))[0] != "EXACT":
            continue
        _nc, r = name_evidence(model, votes, cls, tgt, pa, ppd, rpd, drop_src=src)
        if r is None:
            nref += 1
        else:
            name_items.append((ra, r == ra))

    pos_exact = sum(1 for c in cls.values() if c[0] == "EXACT")
    pos_adm = sum(1 for c in cls.values() if c[0] != "REFUSED")
    shift_exact = sum(1 for ci, v in votes.items() if model.classify(ci, v, shift=4)[0] == "EXACT")
    rr = [ra for _s, _p, ra in apairs]
    random.Random(int(E.get("shuffle_seed", 1313))).shuffle(rr)
    spairs = [(s, pa, rr[k]) for k, (s, pa, _r) in enumerate(apairs)]
    sanchor = {pa: ra for _s, pa, ra in spairs}
    _sv, scls, snames, _st, _sal = run_all(model, spairs, prd, rrd, psize, syms, ppd, rpd, sanchor, hold_src=True)
    shuf_adm = sum(1 for c in scls.values() if c[0] != "REFUSED")
    shuf_named = sum(1 for n in snames.values() if n[0].startswith("NAMED"))
    pos_named = max(1, sum(1 for n in names.values() if n[0].startswith("NAMED")))

    g1 = ctx.engine.judge("E1_EVIDENCE_BYTES", bytes_items, control=(pos_exact, shift_exact),
                          extra={"refused": refused, "anchor_pairs": len(apairs), "aligned_for_operands": aligned,
                                 "control_kind": "every voted delta shifted +4: EXACT contributions admitted"})
    g2 = ctx.engine.judge("E1_EVIDENCE_NAMES", name_items, control=(pos_adm, shuf_adm),
                          require={"shuffle_named_le_0.1pct": shuf_named <= 0.001 * pos_named},
                          extra={"refused": nref, "shuffle_named": shuf_named, "positive_named": pos_named,
                                 "control_kind": "anchor pairing shuffled: contributions admitted"})

    out_rows = []
    for sy in syms:
        ci = model.find(sy["va"])
        c = conts[ci] if ci is not None else None
        bc, d, _ = cls.get(ci, ("REFUSED", None, 0)) if ci is not None else ("REFUSED", None, 0)
        nc, nr = names[sy["i"]]
        retail = sy["va"] + d if bc != "REFUSED" else nr
        if bc != "REFUSED" and nr is not None and nr != retail:
            nc = "REFUSED_CONFLICT_WITH_BYTES"
        out_rows.append((sy["va"], retail, sy["size"], sy["k"], sy["name"],
                         (c["mod"] if c else "-") + ("@%08X" % c["va"] if c else ""), bc, nc))
    out_rows.sort(key=lambda r: (r[0], r[3], r[4]))
    text = table_text(HEADER, [("0x%08X" % r[0], "" if r[1] is None else "0x%08X" % r[1], "" if r[2] is None else str(r[2]),
                                r[3], r[4].replace("\t", " "), r[5], r[6], r[7]) for r in out_rows])
    counts = collections.Counter(r[6] for r in out_rows)
    summary = {"rows": len(out_rows), "bytes_class": dict(sorted(counts.items())), "contributions": len(conts),
               "voted_contributions": len(votes), "positive_exact": pos_exact, "positive_admitted": pos_adm,
               "shift4_exact": shift_exact, "shuffle_admitted": shuf_adm, "shuffle_named": shuf_named,
               "evidence_map_sha256_uncompressed": sha256_bytes(text), "gate": g1["gate"] and g2["gate"]}
    if not summary["gate"]:
        return summary, None
    path = ctx.work / "evidence_map.tsv.gz"
    ctx.writer.write(path, text, gz=True)
    return summary, path
