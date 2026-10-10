"""Stage A: allocate every registered retail function to its prototype compiland (obj) and source
file. Tiers run in declared order (strongest first); a tier fills only what earlier tiers left and
admits only when its gate passes. DS2 measured numbers are in README.md.

  A1 PDB_EXACT / PDB_DELTA  evidence-map proc row at the start (bytes EXACT / DELTA), a keyed feed
                            row, or the interior of a placed proc (name parent+offset)
  A2 LINK_BRACKET           both bracketing anchors carry one delta d; the code contribution at x-d
  A3 LINK_RANGE             inside exactly one contribution whose anchors all imply one retail range
  A4 PDB_BYTES              strict masked bytes + formed addresses mapped through the evidence map +
                            direct-call objs vs the procs of the bracketing objs (tiers: narrow;
                            _GAP: contribution gap; _NOCALL: callee-less allowed)
  A5 INFERRED               nearest allocated neighbours on both sides agree on one obj (fixpoint)
  A6 CLOSEOUT               masked-byte index over every proc + S_THUNK32 with verified refs:
                            LINKER_GEN / IMPORT_THUNK / COMPILER_GEN / PDB_REFS (one owner) /
                            FOLDED (>= 2 owners: owner list is the claim; primary = bracketing obj)
  A7 PDB_DATA_OWNER         data refs (mapped to the PDB data contributions) meet the masked-byte
                            candidates in exactly one obj and no allocated callee contradicts
  A8 MANUAL_EVIDENCE        hand-reviewed accepts (input table); must agree with every method
  A9 UNDETERMINABLE         input list of functions every method refused (with refusal evidence)
Experimental (opt-in; refused on DS2): CALLGRAPH_TIEBREAK, FOLDED_TRACED (caller locality),
LAYOUT_RUN, DATA_REFS, AUX_BYTES (aux PDBs bytes-only), RETAIL_ONLY_FOLDER; measurement-only:
ICF_LEADER, PADDING.
"""
from __future__ import annotations

import bisect, collections, csv, random, re, subprocess

from ..arch import ppc
from ..gates import unique
from ..util import tsv

HEADER = ["addr", "symbol", "class", "basis", "obj", "lib", "src_file", "src_line", "tree_path",
          "name_status", "pdb_name", "ident", "calltype", "nparams", "has_this", "mangled", "prototype"]
FEED_KINDS = {"pdata_proc", "seq_aligned_proc", "start_no_pdata"}
BYTES_BUCKETS = (0, 8, 16, 24, 32, 48, 64, 96, 128, 256)
EXPERIMENTAL = ("CALLGRAPH_TIEBREAK", "FOLDED_TRACED", "LAYOUT_RUN", "DATA_REFS", "AUX_BYTES", "ICF_LEADER", "PADDING",
                "RETAIL_ONLY_FOLDER")
STAGES = ("PDB_EXACT", "LINK_BRACKET", "LINK_RANGE", "PDB_BYTES", "PDB_BYTES_GAP", "PDB_BYTES_NOCALL", "INFERRED",
          "CALLGRAPH_TIEBREAK", "CLOSEOUT", "FOLDED_TRACED", "LAYOUT_RUN", "DATA_REFS", "PDB_DATA_OWNER",
          "MANUAL_EVIDENCE", "RETAIL_ONLY_FOLDER", "UNDETERMINABLE")


def demangle(undname, names):
    out = {}
    names = sorted(set(n for n in names if n.startswith("?")))
    if not undname or not names:
        return out
    for i in range(0, len(names), 200):
        r = subprocess.run([undname] + names[i:i + 200], capture_output=True, text=True, errors="replace")
        for m in re.finditer(r'Undecoration of :- "(.*)"\r?\nis :- "(.*)"', r.stdout):
            out[m.group(1)] = m.group(2)
    return out


def neighbour_vote(idx, keys, alloc_obj, pos, shift=0):
    p = pos + shift
    if p < 0 or p >= len(keys):
        return None
    i = bisect.bisect_left(idx, p)
    lo = idx[i - 1] if i > 0 else None
    j = i + 1 if i < len(idx) and idx[i] == p else i
    hi = idx[j] if j < len(idx) else None
    if lo is None or hi is None:
        return None
    a, b = alloc_obj[keys[lo]], alloc_obj[keys[hi]]
    return a if a == b else None


class Closeout:
    """Masked-byte index over procs and thunks, with a reference check per hit."""

    def __init__(s, rrd, prd, entries, anchors, addr_map, extent, bracket_objs, bracket_proto):
        s.rrd, s.prd, s.anchors, s.extent = rrd, prd, anchors, extent
        s.bracket_objs, s.bracket_proto = bracket_objs, bracket_proto
        s.addr_map, s.addr_keys = addr_map, sorted(addr_map)
        s.proto_of_retail = {ra: ra - d for ra, (d, _o) in anchors.items()}
        s.index, s.sizes, s.pbytes = collections.defaultdict(list), set(), {}
        for e in entries:
            n = int(e["size"] or 0)
            if n < 4 or n % 4:
                continue
            b = prd(int(e["proto_va"], 16), n)
            if b and len(b) == n:
                s.pbytes[e["proto_va"]] = b
                s.index[(n, ppc.masked_sig(b))].append(e)
                s.sizes.add(n)

    def refs(s, x, rb, pva, pb):
        ok = 0
        hp, hr = {}, {}
        for k in range(0, len(pb) - 3, 4):
            wp, wr = ppc.word(pb, k), ppc.word(rb, k)
            op, rd, ra = wp >> 26, (wp >> 21) & 31, (wp >> 16) & 31
            if op == 18 and not wp & 2:
                tp = (pva + k + ((wp & 0x03FFFFFC) ^ 0x02000000) - 0x02000000) & 0xFFFFFFFF
                tr = (x + k + ((wr & 0x03FFFFFC) ^ 0x02000000) - 0x02000000) & 0xFFFFFFFF
                if tr in s.proto_of_retail:
                    if s.proto_of_retail[tr] != tp:
                        return None
                    ok += 1
                continue
            if op == 15 and ra == 0:
                hp[rd], hr[rd] = (wp & 0xFFFF) << 16, (wr & 0xFFFF) << 16
                continue
            if (op == 14 or 32 <= op <= 55) and ra in hp:
                ap = (hp[ra] + ppc.sx16(wp & 0xFFFF)) & 0xFFFFFFFF
                ar = (hr[ra] + ppc.sx16(wr & 0xFFFF)) & 0xFFFFFFFF
                j = bisect.bisect_right(s.addr_keys, ap) - 1
                if not (j >= 0 and ap - s.addr_keys[j] < 0x100):
                    return None
                if ap + s.addr_map[s.addr_keys[j]] - s.addr_keys[j] != ar:
                    return None
                ok += 1
            if op in (14, 15) or 32 <= op <= 47:
                hp.pop(rd, None), hr.pop(rd, None)
        return ok

    def hits(s, x, exclude=None, shift=0):
        ext = s.extent(x)
        out = []
        for n in range(max(4, ext - 12), ext + 1, 4):
            if n not in s.sizes:
                continue
            rb = s.rrd(x + shift, n)
            if not rb or len(rb) != n:
                continue
            for e in s.index.get((n, ppc.masked_sig(rb)), ()):
                if e["proto_va"] == exclude:
                    continue
                r = s.refs(x + shift, rb, int(e["proto_va"], 16), s.pbytes[e["proto_va"]])
                if r is not None:
                    out.append((e, r))
        return out

    def classify(s, x, exclude=None, shift=0):
        hs = s.hits(x, exclude, shift)
        if not hs:
            return None
        owners = sorted({e["obj"] for e, _ in hs})
        refs = max(r for _, r in hs)
        kinds = {e["kind"] for e, _ in hs}
        if all("* Linker *" in o for o in owners):
            cls = "LINKER_GEN"
        elif all(o.startswith("Import:") for o in owners):
            cls = "IMPORT_THUNK"
        elif kinds == {"thunk"}:
            cls = "COMPILER_GEN"
        elif len(owners) == 1 and refs >= 1:
            cls = "PDB_REFS"
        elif len(owners) == 1:
            return None
        else:
            cls = "FOLDED"
        if len(owners) == 1:
            primary = owners[0]
        else:
            br = [o for o in owners if o in s.bracket_objs(x)]
            if len(br) == 1:
                primary = br[0]
            else:
                bp = s.bracket_proto(x)
                if bp is None:
                    return None
                primary = min(((abs(int(e["proto_va"], 16) - bp), e["obj"]) for e, _ in hs))[1]
        single = [e for e, _ in hs if e["obj"] == primary]
        return cls, primary, owners, (single[0] if len({e["proto_va"] for e in single}) == 1 else None), refs

    def measure(s, known, sample):
        """-> {key: [(x, correct)]}: positive_<cls> (own proc included; FOLDED: truth in owner
        list), loo_unique (own proc hidden), negative_shift4 (accepted counts as an item)."""
        st = collections.defaultdict(list)
        for x in sample:
            d, truth = known[x]
            own = "0x%08X" % (x - d)
            c = s.classify(x)
            if c:
                cls, prim, owners, _e, _r = c
                st["positive_" + cls].append((x, (truth in owners) if cls == "FOLDED" else (prim == truth)))
                if cls == "FOLDED":
                    st["positive_FOLDED_primary"].append((x, prim == truth))
            c = s.classify(x, exclude=own)
            if c and len(c[2]) == 1:
                st["loo_unique"].append((x, c[1] == truth))
            st["negative_shift4"].append((x, s.classify(x, shift=4) is not None))
        return st


def counts(items):
    return {"n": len(items), "agree": sum(1 for _, ok in items if ok)}


def run(cfg, ctx, reg, P, aux, evmap_path, feed_path):
    """P: primary tables {procs, contribs, dcontribs, thunks} (lists of dicts), aux: {label: (P, rd)}.
    Returns (rows, owners_of, report)."""
    A = cfg.get("allocation", {})
    exp = set(A.get("experimental", []))
    G = ctx.engine
    rrd, prd = ctx.rrd, ctx.prd
    funcs = reg.funcs
    addrs = [x for x, _ in funcs]
    report = {"stages": {}, "experimental_enabled": sorted(exp)}

    procs = collections.defaultdict(list)
    for r in P["procs"]:
        procs[int(r["proto_va"], 16)].append(r)
    starts, extents, mangled_by_proto = collections.defaultdict(list), [], collections.defaultdict(set)
    anchor_rows, addr_map_rows = collections.defaultdict(set), collections.defaultdict(set)
    contribs = sorted((int(c["proto_va"], 16), int(c["size"]), c["obj"], c["lib"]) for c in P["contribs"])
    evrows = 0
    for r in tsv(evmap_path):
        if r["kind"] == "public_code" and r["proto_addr"]:
            mangled_by_proto[int(r["proto_addr"], 16)].add(r["name"])
        if r["retail_addr"] and r["proto_addr"] and r["bytes_class"] in ("EXACT", "DELTA"):
            addr_map_rows[int(r["proto_addr"], 16)].add(int(r["retail_addr"], 16))
        if (r["kind"] in ("proc", "public_code", "thunk") and r["retail_addr"] and r["proto_addr"]
                and r["bytes_class"] in ("EXACT", "DELTA")):
            ra = int(r["retail_addr"], 16)
            anchor_rows[ra].add((ra - int(r["proto_addr"], 16), r["contribution"].rsplit("@", 1)[0]))
        if r["kind"] != "proc" or not r["retail_addr"] or r["bytes_class"] not in ("EXACT", "DELTA"):
            continue
        evrows += 1
        ra, pa = int(r["retail_addr"], 16), int(r["proto_addr"], 16)
        starts[ra].append(("PDB_" + r["bytes_class"], "evmap_" + r["name_class"].lower(), pa))
        if r["size"]:
            extents.append((ra, ra + int(r["size"]), "PDB_" + r["bytes_class"], pa))
    feedrows = 0
    if feed_path:
        for r in tsv(feed_path):
            if r["kind"] in FEED_KINDS and r["proto_addr"] and r["retail_addr"]:
                ra, pa = int(r["retail_addr"], 16), int(r["proto_addr"], 16)
                if not any(c[2] == pa for c in starts.get(ra, [])):
                    starts[ra].append(("PDB_DELTA", "v2_" + r["kind"], pa))
                    feedrows += 1
    extents.sort()
    ext_starts = [e[0] for e in extents]
    lane5 = {}
    if ctx.inputs.get("mangled_names"):
        for r in tsv(ctx.inputs["mangled_names"]):
            if r.get("mangled", "-") not in ("-", ""):
                lane5.setdefault(int(r["va"], 16), set()).add(r["mangled"])

    rows, alloc_obj = {}, {}
    # ---- A1 PDB_EXACT / PDB_DELTA ----
    for addr, _sym in funcs:
        cands = starts.get(addr, [])
        if cands:
            ps = [(c, p) for c in cands for p in procs.get(c[2], [])]
            objs = {p["obj"] for _, p in ps}
            if len(objs) == 1:
                cls = "PDB_EXACT" if any(c[0] == "PDB_EXACT" for c, _ in ps) else "PDB_DELTA"
                srcs = sorted({p["src_file"] for _, p in ps if p["src_file"]})
                p0 = sorted(ps, key=lambda cp: (cp[1]["src_file"] == "", cp[1]["proto_va"]))[0][1]
                rows[addr] = dict(cls=cls, basis=sorted({c[1] for c, _ in ps})[0], p=p0,
                                  src=srcs[0] if len(srcs) == 1 else "", names=sorted({p["name"] for _, p in ps}),
                                  pas={c[2] for c, _ in ps}, start=True)
                alloc_obj[addr] = p0["obj"]
                continue
        i = bisect.bisect_right(ext_starts, addr) - 1
        hit = None
        while i >= 0 and extents[i][0] > addr - 0x40000:
            s, e, cls, pa = extents[i]
            if s < addr < e and procs.get(pa):
                hit = (s, cls, pa)
                break
            i -= 1
        if hit:
            s, cls, pa = hit
            ps = procs[pa]
            if len({p["obj"] for p in ps}) == 1:
                p0 = ps[0]
                rows[addr] = dict(cls="PDB_DELTA", basis="interior+0x%X" % (addr - s), p=p0, src=p0["src_file"],
                                  names=["%s+0x%X" % (p0["name"], addr - s)], pas={pa}, start=False)
                alloc_obj[addr] = p0["obj"]
    report["stages"]["PDB_EXACT"] = {"gate": "inherits E1 evidence-map gates", "evmap_proc_rows": evrows,
                                     "feed_rows_added": feedrows, "admitted": len(rows)}

    # ---- link order ----
    anchors = {ra: next(iter(v)) for ra, v in anchor_rows.items() if len(v) == 1}
    ka = sorted(anchors)
    cst = [c[0] for c in contribs]

    def contrib_at(p):
        i = bisect.bisect_right(cst, p) - 1
        return contribs[i] if i >= 0 and contribs[i][0] <= p < contribs[i][0] + contribs[i][1] else None

    def bracket(x, dfn=None):
        i = bisect.bisect_left(ka, x)
        lo, hi = i - 1, i + 1 if i < len(ka) and ka[i] == x else i
        if lo < 0 or hi >= len(ka):
            return None
        (dl, _), (dr, _) = anchors[ka[lo]], anchors[ka[hi]]
        if dl != dr:
            return None
        if dfn:
            dl = dfn(x)
        c = contrib_at(x - dl)
        return (c, x - dl) if c else None

    implied = collections.defaultdict(set)
    for ra, (d, _o) in anchors.items():
        c = contrib_at(ra - d)
        if c:
            implied[c].add((ra, ra - (ra - d - c[0])))
    cranges = sorted((next(iter({s for _, s in v})), c) for c, v in implied.items() if len({s for _, s in v}) == 1)
    crange_starts = [s for s, _ in cranges]

    def in_ranges(x):
        j = bisect.bisect_right(crange_starts, x) - 1
        hits = {c[2] for s, c in cranges[max(0, j - 8):j + 1] if s <= x < s + c[1]}
        return next(iter(hits)) if len(hits) == 1 else None

    proc_at = {int(p["proto_va"], 16): p for ps in procs.values() for p in ps}
    proc_starts = sorted(proc_at)

    def proc_containing(p):
        i = bisect.bisect_right(proc_starts, p) - 1
        if i >= 0:
            q = proc_at[proc_starts[i]]
            if proc_starts[i] <= p < proc_starts[i] + max(int(q["size"] or 0), 1):
                return q
        return None

    # controls for A2/A3: the same rule in a world where each code contribution's obj label is
    # replaced by a random other contribution's (chance level of hitting the truth obj)
    rng_c = random.Random(int(A.get("control_seed", 20261007)))
    perm = [c[2] for c in contribs]
    rng_c.shuffle(perm)
    perm_of = {c: perm[i] for i, c in enumerate(contribs)}
    # A2 LINK_BRACKET: LOO = hide the anchor, re-derive its obj from its brackets
    # control: contribution obj labels permuted (chance level of hitting the truth obj). DS2 chance
    # floor 418/29,380 = 1.4%: objs own many scattered contributions (COMDAT order), so the bar is
    # an override (logged). Rejected as not must-fail on DS2: the delta of the anchor 97 positions
    # away (19,967/21,535: deltas differ by bytes, same obj) and delta +0x20000 (6,423/29,380).
    items, ctrl_n, ctrl_a = [], 0, 0
    for x in ka:
        truth = anchors[x][1]
        b = bracket(x)
        if b:
            items.append((x, b[0][2] == truth))
            ctrl_n += 1
            ctrl_a += perm_of[b[0]] == truth
    gb = G.judge("A2_LINK_BRACKET", items, control=(ctrl_n, ctrl_a),
                 extra={"control_kind": "contribution obj labels permuted: truth obj hit"})
    # A3 LINK_RANGE: rebuild a contribution's range without each of its anchors
    items, ctrl_n, ctrl_a = [], 0, 0
    for (s, c) in cranges:
        mem = implied[c]
        if len(mem) < 2:
            continue
        for ra, _s in mem:
            if len({s2 for r2, s2 in mem if r2 != ra}) == 1:
                o = in_ranges(ra)
                items.append((ra, o == anchors[ra][1]))
                if o is not None:
                    ctrl_n += 1
                    ctrl_a += perm_of[c] == anchors[ra][1]
    gr = G.judge("A3_LINK_RANGE", items, control=(ctrl_n, ctrl_a),
                 extra={"control_kind": "contribution obj labels permuted: truth obj hit"})
    link = {}
    for x in addrs:
        if x in alloc_obj:
            continue
        b = bracket(x) if gb["gate"] else None
        if b:
            c, p = b
            q = proc_containing(p)
            q = q if q and q["obj"] == c[2] else None
            link[x] = ("LINK_BRACKET", "delta=%d;proto=0x%08X" % (x - p, p), c[2], c[3], q)
            continue
        o = in_ranges(x) if gr["gate"] else None
        if o:
            link[x] = ("LINK_RANGE", "contribution_range", o, "", None)
    for x, v in link.items():
        alloc_obj[x] = v[2]
    report["stages"]["LINK_BRACKET"] = {"admitted": sum(v[0] == "LINK_BRACKET" for v in link.values()),
                                        "anchors": len(anchors), "code_contributions": len(contribs)}
    report["stages"]["LINK_RANGE"] = {"admitted": sum(v[0] == "LINK_RANGE" for v in link.values()),
                                      "mapped_contribution_ranges": len(cranges)}

    # ---- A4 PDB_BYTES ----
    procs_by_obj = collections.defaultdict(list)
    for ps in procs.values():
        for p in ps:
            if int(p["size"] or 0) >= 8:
                procs_by_obj[p["obj"]].append(p)
    pbytes = {}
    addr_map = {k: next(iter(v)) for k, v in addr_map_rows.items() if len(v) == 1}
    addr_keys = sorted(addr_map)
    gap_contribs = int(A.get("bytes_gap_contribs", 8))
    refs_optional = [False]

    def proto_bytes(p):
        k = p["proto_va"]
        if k not in pbytes:
            pbytes[k] = prd(int(k, 16), int(p["size"]))
        return pbytes[k]

    def cand_objs(x, gap=None):
        i = bisect.bisect_left(ka, x)
        lo, hi = i - 1, i + 1 if i < len(ka) and ka[i] == x else i
        if lo < 0 or hi >= len(ka):
            return set()
        (dl, ol), (dr, orr) = anchors[ka[lo]], anchors[ka[hi]]
        out = {c[2] for c in (contrib_at(x - dl), contrib_at(x - dr)) if c} | {ol, orr}
        pl, pr_ = sorted((ka[lo] - dl, ka[hi] - dr))
        i0 = max(0, bisect.bisect_right(cst, pl) - 1 - (gap or 0))
        i1 = min(len(contribs), bisect.bisect_right(cst, pr_) + (gap or 0))
        if gap is not None and i1 - i0 <= 400:
            out |= {contribs[k][2] for k in range(i0, i1)}
        return out

    def addresses_agree(pb, rb):
        for ap, ar in ppc.formed_pairs(pb, rb):
            j = bisect.bisect_right(addr_keys, ap) - 1
            if j >= 0 and ap - addr_keys[j] < 0x100:
                if ap + addr_map[addr_keys[j]] - addr_keys[j] != ar:
                    return False
            else:
                return False
        return True

    def proto_obj(pva):
        c = contrib_at(pva)
        return c[2] if c else None

    def refs_agree(x, rb, pva, pb, exclude_va):
        agree = 0
        hidden = int(exclude_va, 16) if exclude_va else None
        for tr, tp in ppc.bl_pairs(rb, pb, x, pva):
            orr = alloc_obj.get(tr) if tr != x else None
            if orr is None or (hidden is not None and tr in anchors and tr - anchors[tr][0] == hidden):
                continue
            op = proto_obj(tp)
            if op is None:
                continue
            if op != orr:
                return False
            agree += 1
        return agree >= 1 or (refs_optional[0] and agree == 0)

    def byte_match(x, objs, exclude_va=None, shift=0):
        hits = []
        for o in sorted(objs):
            for p in procs_by_obj.get(o, ()):
                if p["proto_va"] == exclude_va:
                    continue
                pb = proto_bytes(p)
                rb = rrd(x + shift, len(pb)) if pb else None
                if pb and rb and ppc.strict_equal_words(pb, rb) and addresses_agree(pb, rb) \
                        and refs_agree(x + shift, rb, int(p["proto_va"], 16), pb, exclude_va):
                    hits.append(p)
        return hits

    byte_buckets = collections.defaultdict(lambda: [0, 0])
    tiers = [("PDB_BYTES", None, False)]
    for t, g, nc in (("PDB_BYTES_GAP", gap_contribs, False), ("PDB_BYTES_NOCALL", None, True)):
        if t not in A.get("disable", []):
            tiers.append((t, g, nc))
    ctrl_every = int(A.get("bytes_control_every", 16))
    # Known answers: the anchors, plus (A.bytes_known = "anchors+bracket", default) every A2
    # LINK_BRACKET function whose mapped start is exactly a proc start of its obj (A2 is gated at
    # 29,378/29,380; any label noise can only lower the measurement). Same shape: functions
    # between anchors, the population A4 fills.
    ka_known = [(x, anchors[x][1], "0x%08X" % (x - anchors[x][0])) for x in ka]
    if A.get("bytes_known", "anchors+bracket") == "anchors+bracket":
        for x in addrs:
            lk = link.get(x)
            if lk and lk[0] == "LINK_BRACKET" and lk[4] and x not in anchors:
                pv = int(lk[1].split("proto=")[1], 16)
                if lk[4]["proto_va"] == "0x%08X" % pv:
                    ka_known.append((x, lk[2], lk[4]["proto_va"]))
        ka_known.sort()
    report["stages"].setdefault("PDB_BYTES_known", {})["n"] = len(ka_known)
    for tier, gap, nocall in tiers:
        refs_optional[0] = nocall
        items, cn, ca = [], 0, 0
        for k_, (x, truth, own) in enumerate(ka_known):
            hits = byte_match(x, cand_objs(x, gap), exclude_va=own)
            objs = {p["obj"] for p in hits}
            if len(objs) == 1:
                got = next(iter(objs))
                items.append((x, got == truth))
                if tier == "PDB_BYTES":
                    sz = min(int(p["size"]) for p in hits)
                    byte_buckets["ge%d" % max(b for b in BYTES_BUCKETS if sz >= b)][0] += 1
                    byte_buckets["ge%d" % max(b for b in BYTES_BUCKETS if sz >= b)][1] += got == truth
            if k_ % ctrl_every == 0:
                cn += 1
                ch = byte_match(x, cand_objs(x, gap), exclude_va=own, shift=4)
                ca += len({p["obj"] for p in ch}) == 1
        g = G.judge("A4_" + tier, items, control=(cn, ca),
                    extra={"control_kind": "retail bytes read at x+4 (every %dth anchor): unique obj accepted" % ctrl_every})
        adm = 0
        if g["gate"]:
            for x in addrs:
                if x in alloc_obj:
                    continue
                hits = byte_match(x, cand_objs(x, gap))
                if len({p["obj"] for p in hits}) == 1:
                    q = hits[0] if len(hits) == 1 else None
                    link[x] = (tier, "bytes+addresses+calls;matches=%d" % len(hits), hits[0]["obj"], hits[0]["lib"], q)
                    alloc_obj[x] = hits[0]["obj"]
                    adm += 1
        report["stages"][tier] = {"admitted": adm}
    report["stages"]["PDB_BYTES"]["loo_by_min_size"] = {k: {"n": v[0], "agree": v[1]} for k, v in sorted(byte_buckets.items())}

    # ---- A5 INFERRED ----
    keys = addrs
    pos = {x: k for k, x in enumerate(keys)}
    idx = sorted(pos[x] for x in alloc_obj)
    obj_src = collections.defaultdict(collections.Counter)
    obj_lib = {}
    for r in rows.values():
        if r["src"]:
            obj_src[r["p"]["obj"]][r["src"]] += 1
        obj_lib[r["p"]["obj"]] = r["p"]["lib"]
    items, sh_n, sh_ok = [], 0, 0
    shift = int(A.get("inferred_shift", 97))
    for x in alloc_obj:
        v = neighbour_vote(idx, keys, alloc_obj, pos[x])
        if v is not None:
            items.append((x, v == alloc_obj[x]))
        v = neighbour_vote(idx, keys, alloc_obj, pos[x], shift)
        if v is not None:
            sh_n += 1
            sh_ok += v == alloc_obj[x]
    rng = random.Random(int(A.get("shuffle_seed", 20261006)))
    labs = [alloc_obj[keys[k]] for k in idx]
    rng.shuffle(labs)
    shuf = {keys[k]: l for k, l in zip(idx, labs)}
    sf_n = sf_ok = 0
    for x in alloc_obj:
        v = neighbour_vote(idx, keys, shuf, pos[x])
        if v is not None:
            sf_n += 1
            sf_ok += v == shuf[x]
    gi = G.judge("A5_INFERRED", items, control=(sf_n, sf_ok),
                 extra={"control_kind": "same rule over randomly permuted obj labels",
                        "shift_%d" % shift: {"n": sh_n, "agree": sh_ok}})
    for c in contribs:
        obj_lib.setdefault(c[2], c[3])
    inferred = {}
    if gi["gate"]:
        for _round in range(8):
            new = {}
            for x in keys:
                if x in alloc_obj:
                    continue
                v = neighbour_vote(idx, keys, alloc_obj, pos[x])
                if v is not None:
                    new[x] = v
            if not new:
                break
            inferred.update(new)
            alloc_obj.update(new)
            idx = sorted(pos[x] for x in alloc_obj)
    report["stages"]["INFERRED"] = {"admitted": len(inferred)}

    calls, callees_of = reg.calls, reg.callees

    # ---- experimental: CALLGRAPH_TIEBREAK ----
    def two_delta_candidates(x):
        i = bisect.bisect_left(ka, x)
        lo, hi = i - 1, i + 1 if i < len(ka) and ka[i] == x else i
        if lo < 0 or hi >= len(ka):
            return None
        (dl, ol), (dr, orr) = anchors[ka[lo]], anchors[ka[hi]]
        cand = {c[2] for c in (contrib_at(x - dl), contrib_at(x - dr)) if c} | {ol, orr}
        return cand if len(cand) > 1 else None

    def affinity(x, cand, known_obj):
        score = collections.Counter()
        for t in calls.get(x, ()):
            o = known_obj.get(t)
            if o in cand:
                score[o] += 1
        if not score:
            return None
        best = score.most_common(2)
        return best[0][0] if len(best) == 1 or best[0][1] > best[1][1] else None

    if "CALLGRAPH_TIEBREAK" in exp:
        items = []
        for x in ka:
            if bracket(x):
                continue
            cand = two_delta_candidates(x)
            if not cand or anchors[x][1] not in cand:
                continue
            known_wo = dict(alloc_obj)
            known_wo.pop(x, None)
            v = affinity(x, cand, known_wo)
            if v is not None:
                items.append((x, v == anchors[x][1]))
        g = G.judge("X_CALLGRAPH_TIEBREAK", items, control=None,
                    require={"control_defined": False}, extra={"note": "DS2: 360/389 = 0.925, refused"})
        tie = {}
        if g["gate"]:
            for x in keys:
                if x in alloc_obj:
                    continue
                cand = two_delta_candidates(x)
                v = affinity(x, cand, alloc_obj) if cand else None
                if v is not None:
                    tie[x] = v
            for x, v in tie.items():
                alloc_obj[x] = v
                link[x] = ("LINK_CALLGRAPH", "two_delta+call_affinity", v, obj_lib.get(v, ""), None)
        report["stages"]["CALLGRAPH_TIEBREAK"] = {"admitted": len(tie)}

    # ---- A6 CLOSEOUT ----
    reg_sorted = sorted(addrs)
    nxt_of = dict(zip(reg_sorted, reg_sorted[1:]))
    EXTENTS = A.get("extents", "registered")  # "normalised": A6/A7 read the X0 extent table
    if EXTENTS == "normalised":
        from ..extents import for_ctx
        _reg = {x: nxt_of.get(x, x + 4) - x for x in reg_sorted}
        _ext = for_ctx(ctx, reg_sorted, _reg)[0]
        nxt_of = {x: x + _ext[x] for x in reg_sorted}
    elif EXTENTS != "registered":
        raise SystemExit("REFUSED: allocation.extents must be registered|normalised, got %r" % EXTENTS)
    report["extents"] = EXTENTS
    entries = [dict(p, kind="proc") for ps in procs.values() for p in ps]
    for t in P["thunks"]:
        entries.append(dict(t, kind="thunk", src_file="", src_line="", calltype="", nparams="", has_this=""))

    def bracket_proto(x):
        i = bisect.bisect_left(ka, x)
        return x - anchors[ka[i - 1]][0] if i > 0 else None

    co = Closeout(rrd, prd, entries, anchors, addr_map, lambda x: nxt_of.get(x, x + 4) - x,
                  lambda x: cand_objs(x), bracket_proto)
    sample = sorted(ka)
    random.Random(int(A.get("closeout_seed", 20261006))).shuffle(sample)
    sample = sample[:int(A.get("closeout_sample", 8000))]
    co_m = co.measure(anchors, sample)
    neg = co_m["negative_shift4"]
    negc = (len(neg), sum(1 for _, a in neg if a))
    co_ok = {}
    for cls in ("LINKER_GEN", "IMPORT_THUNK", "COMPILER_GEN", "PDB_REFS", "FOLDED"):
        co_ok[cls] = G.judge("A6_CLOSEOUT_" + cls, co_m.get("positive_" + cls, []), control=negc,
                             extra={"control_kind": "retail bytes shifted +4: any class accepted",
                                    "claim": "owner list holds truth" if cls == "FOLDED" else "unique owner = truth"})["gate"]
    unique_ok = G.judge("A6_CLOSEOUT_UNIQUE_LOO", co_m.get("loo_unique", []), control=negc,
                        extra={"control_kind": "retail bytes shifted +4"})["gate"]
    owners_of = {}
    adm = collections.Counter()
    for x in [x for x in keys if x not in alloc_obj]:
        c = co.classify(x)
        if not c:
            continue
        cls, prim, owners, e, refs = c
        if not co_ok.get(cls) or (len(owners) == 1 and not unique_ok):
            continue
        q = e if e and e.get("kind") == "proc" else None
        basis = "masked_bytes;refs=%d;owners=%d" % (refs, len(owners))
        if e and e.get("kind") == "thunk":
            basis += ";thunk=" + e["name"][:80].replace("\t", " ")
        link[x] = (cls, basis, prim, obj_lib.get(prim, ""), q)
        alloc_obj[x] = prim
        adm[cls] += 1
        if len(owners) > 1:
            owners_of[x] = owners
    report["stages"]["CLOSEOUT"] = {"admitted": dict(sorted(adm.items())),
                                    "measure": {k: counts(v) for k, v in sorted(co_m.items())}}

    def masked_objs(x, shift=0):
        ext = nxt_of.get(x, x + 4) - x
        objs = set()
        for n in range(max(4, ext - 12), ext + 1, 4):
            b = rrd(x + shift, n)
            if b and len(b) == n:
                objs |= {e["obj"] for e in co.index.get((n, ppc.masked_sig(b)), ())}
        return objs

    pool = None

    def get_pool():
        nonlocal pool
        if pool is None:
            pool = [x for x in ka if len(masked_objs(x)) >= 2]
        return pool

    # ---- experimental: FOLDED_TRACED ----
    if "FOLDED_TRACED" in exp:
        def traced_owners(x, known_obj, neigh=None, shift=0):
            mo = masked_objs(x, shift)
            if len(mo) < 2:
                return None
            nb = calls.get(x, ()) if neigh is None else neigh
            own = sorted(mo & {known_obj[t] for t in nb if t != x and t in known_obj})
            return own or None
        items, cs_n, cs_a = [], 0, 0
        rng_ft = random.Random(99)
        for x in get_pool():
            truth = anchors[x][1]
            o = traced_owners(x, alloc_obj)
            if o is not None:
                items.append((x, truth in o))
            y = rng_ft.choice(ka)
            o = traced_owners(x, alloc_obj, neigh=calls.get(y, ()))
            cs_n += 1
            cs_a += o is not None and truth not in o
        g = G.judge("X_FOLDED_TRACED", items, control=(cs_n, cs_a),
                    require={"n_ge_100": len(items) >= 100},
                    extra={"control_kind": "shuffled callers", "note": "DS2: 1627/1930 = 0.843, control 15%, refused"})
        k = 0
        if g["gate"]:
            for x in [x for x in keys if x not in alloc_obj]:
                o = traced_owners(x, alloc_obj)
                if not o:
                    continue
                br = [w for w in o if w in cand_objs(x)]
                prim = br[0] if len(br) == 1 else o[0]
                link[x] = ("FOLDED_TRACED", "masked_bytes+traced_refs;owners=%d" % len(o), prim, obj_lib.get(prim, ""), None)
                alloc_obj[x] = prim
                owners_of[x] = o
                k += 1
        report["stages"]["FOLDED_TRACED"] = {"admitted": k}

    # ---- experimental: LAYOUT_RUN ----
    if "LAYOUT_RUN" in exp:
        runs = collections.defaultdict(list)
        for y, o in alloc_obj.items():
            runs[o].append(y)
        for o in runs:
            runs[o].sort()

        def inside_run(o, x):
            r = runs.get(o)
            if not r:
                return False
            first = r[0] if r[0] != x else (r[1] if len(r) > 1 else None)
            last = r[-1] if r[-1] != x else (r[-2] if len(r) > 1 else None)
            return first is not None and last is not None and first < x < last

        def layout_owner(x, cands):
            hit = [o for o in sorted(cands) if inside_run(o, x)]
            return hit[0] if len(hit) == 1 else None
        items, cn, ca = [], 0, 0
        all_objs = sorted(runs)
        rng_lr = random.Random(5)
        for x in get_pool():
            truth = alloc_obj.get(x)
            cands = masked_objs(x)
            o = layout_owner(x, cands)
            if o is not None:
                items.append((x, o == truth))
            o = layout_owner(x, set(rng_lr.sample(all_objs, len(cands))) - {truth})
            cn += 1
            ca += o is not None
        g = G.judge("X_LAYOUT_RUN", items, control=(cn, ca), require={"n_ge_100": len(items) >= 100},
                    extra={"control_kind": "shuffled candidates", "note": "DS2: 515/618 = 0.833, refused"})
        k = 0
        if g["gate"]:
            for x in [x for x in keys if x not in alloc_obj]:
                cands = masked_objs(x)
                if len(cands) >= 2:
                    o = layout_owner(x, cands)
                    if o:
                        link[x] = ("LAYOUT_RUN", "fold_candidates=%d;inside_one_run" % len(cands), o, obj_lib.get(o, ""), None)
                        alloc_obj[x] = o
                        owners_of[x] = sorted(cands)
                        k += 1
        report["stages"]["LAYOUT_RUN"] = {"admitted": k}

    # ---- data references (shared by DATA_REFS and PDB_DATA_OWNER) ----
    inv = sorted((r, p) for p, r in addr_map.items())
    inv_keys = [r for r, _ in inv]
    dcon = sorted((int(c["proto_va"], 16), int(c["size"]), c["obj"]) for c in P["dcontribs"])
    dstarts = [c[0] for c in dcon]

    def data_obj(ar):
        j = bisect.bisect_right(inv_keys, ar) - 1
        if j < 0 or ar - inv_keys[j] >= 0x100:
            return None
        p = ar - (inv[j][0] - inv[j][1])
        i = bisect.bisect_right(dstarts, p) - 1
        return dcon[i][2] if i >= 0 and dcon[i][0] <= p < dcon[i][0] + dcon[i][1] else None

    def refs_of(x):
        return ppc.formed(rrd(x, nxt_of.get(x, x + 4) - x) or b"")

    if "DATA_REFS" in exp:
        def data_vote(lst):
            objs = {data_obj(r) for r in lst} - {None}
            return next(iter(objs)) if len(objs) == 1 else None
        items, cn, ca = [], 0, 0
        rng_dr = random.Random(11)
        ref_cache = {x: refs_of(x) for x in sample}
        for x in sample:
            truth = anchors[x][1]
            v = data_vote(ref_cache[x])
            if v is not None:
                items.append((x, v == truth))
            y = rng_dr.choice(sample)
            if y != x and anchors[y][1] != truth:
                cn += 1
                ca += data_vote(ref_cache[y]) == truth
        g = G.judge("X_DATA_REFS", items, control=(cn, ca), require={"n_ge_100": len(items) >= 100},
                    extra={"control_kind": "another function's refs", "note": "DS2: 887/1729 = 0.513, refused"})
        k = 0
        if g["gate"]:
            for x in [x for x in keys if x not in alloc_obj]:
                v = data_vote(refs_of(x))
                if v:
                    link[x] = ("DATA_REFS", "all_mapped_data_refs_in_one_obj", v, obj_lib.get(v, ""), None)
                    alloc_obj[x] = v
                    k += 1
        report["stages"]["DATA_REFS"] = {"admitted": k}

    # ---- A7 PDB_DATA_OWNER ----
    def s1(x, cands, refs, known):
        if any(t in known for t in callees_of.get(x, ())):
            return None
        d = {data_obj(r) for r in refs} - {None}
        d = d & cands if cands else d
        return next(iter(d)) if len(d) == 1 else None

    small = sorted(x for x in alloc_obj if link.get(x, ("",))[0] != "MANUAL_EVIDENCE" and nxt_of.get(x, x + 4) - x <= 64)
    random.Random(3).shuffle(small)
    s1_pop = []
    for x in small:
        c = masked_objs(x)
        if len(c) >= 2:
            s1_pop.append((x, c))
        if len(s1_pop) >= int(A.get("data_owner_population", 4000)):
            break
    rng_s1 = random.Random(13)
    s1_refs = {x: refs_of(x) for x, _c in s1_pop}
    items, cn, ca = [], 0, 0
    for x, c in s1_pop:
        truth = alloc_obj[x]
        v = s1(x, c, s1_refs[x], alloc_obj)
        if v is not None:
            items.append((x, v == truth))
        y, _cy = rng_s1.choice(s1_pop)
        if alloc_obj[y] != truth:
            cn += 1
            ca += s1(x, c, s1_refs[y], alloc_obj) == truth
    g = G.judge("A7_PDB_DATA_OWNER", items, control=(cn, ca), require={"n_ge_100": len(items) >= 100},
                extra={"control_kind": "another function's data refs", "population": "<= 64 bytes, >= 2 masked-byte candidates"})
    k = 0
    if g["gate"]:
        for x in [x for x in keys if x not in alloc_obj]:
            v = s1(x, masked_objs(x), refs_of(x), alloc_obj)
            if v:
                link[x] = ("PDB_DATA_OWNER", "data_refs_meet_candidates_in_one_obj", v, obj_lib.get(v, ""), None)
                alloc_obj[x] = v
                k += 1
    report["stages"]["PDB_DATA_OWNER"] = {"admitted": k}

    # ---- experimental: AUX_BYTES (aux prototype PDBs, bytes only; no address map for them) ----
    if "AUX_BYTES" in exp:
        exact = [(x, rows[x]["src"].lower().replace("\\", "/").rsplit("/", 1)[-1])
                 for x in sorted(rows) if rows[x]["cls"] == "PDB_EXACT" and rows[x]["src"]]
        random.Random(7).shuffle(exact)
        exact = exact[:4000]
        for label, (AP, ard) in sorted(aux.items()):
            index, sizes = collections.defaultdict(set), set()
            for p in AP["procs"]:
                n = int(p["size"] or 0)
                b = ard(int(p["proto_va"], 16), n) if n >= 8 else None
                if b and len(b) == n:
                    index[(n, ppc.masked_sig(b))].add(((p["src_file"] or p["obj"]).lower().replace("\\", "/").rsplit("/", 1)[-1],
                                                       p["src_file"], p["obj"], p["lib"]))
                    sizes.add(n)

            def amatch(x):
                ext = nxt_of.get(x, x + 4) - x
                hits = set()
                for n in range(max(8, ext - 12), ext + 1, 4):
                    if n in sizes:
                        b = rrd(x, n)
                        if b:
                            hits |= index.get((n, ppc.masked_sig(b)), set())
                return min(hits) if len({h[0] for h in hits}) == 1 else None
            its = [(x, m[0] == t) for x, t in exact for m in [amatch(x)] if m]
            g = G.judge("X_AUX_BYTES_" + label, its, control=None, require={"control_defined": False},
                        extra={"truth": "PDB_EXACT rows' source basename", "note": "DS2: q 0.973, d 0.946, refused"})
            k = 0
            if g["gate"]:
                for x in [x for x in keys if x not in alloc_obj]:
                    m = amatch(x)
                    if m:
                        link[x] = ("AUX_BYTES", "aux=%s;bytes_only" % label, m[2], m[3], {"src_file": m[1], "src_line": "",
                                   "name": "", "calltype": "", "nparams": "", "has_this": ""})
                        alloc_obj[x] = m[2]
                        k += 1
            report["stages"]["AUX_BYTES_" + label] = {"admitted": k}

    # ---- experimental measurements that never admit: ICF_LEADER, PADDING ----
    if "ICF_LEADER" in exp:
        cmod = sorted((int(c["proto_va"], 16), int(c["size"]), int(c["module_index"])) for c in P["contribs"])
        cms = [c[0] for c in cmod]
        by_va = collections.defaultdict(dict)
        for p in P["procs"]:
            by_va[int(p["proto_va"], 16)][int(p["module_index"])] = p["obj"]
        its, rnd_n, rnd_a = [], 0, 0
        rng_i = random.Random(7)
        for va, mods in sorted(by_va.items()):
            if len(mods) < 2:
                continue
            i = bisect.bisect_right(cms, va) - 1
            if i < 0 or not (cmod[i][0] <= va < cmod[i][0] + cmod[i][1]) or cmod[i][2] not in mods:
                continue
            ks = sorted(mods)
            its.append((va, ks[0] == cmod[i][2]))
            rnd_n += 1
            rnd_a += rng_i.choice(ks) == cmod[i][2]
        G.judge("X_ICF_LEADER", its, control=(rnd_n, rnd_a),
                extra={"rule": "earliest module in link order survives identical-code folding",
                       "control_kind": "random candidate (chance level)", "admits": "never (would only pick the FOLDED primary)",
                       "note": "DS2: 0.342 vs random 0.386, refused"})
    if "PADDING" in exp:
        bnd, inner = [0, 0], [0, 0]
        for x0, x1 in zip(keys, keys[1:]):
            if x0 not in alloc_obj or x1 not in alloc_obj:
                continue
            b = rrd(x1 - 4, 4)
            if not b:
                continue
            t = bnd if alloc_obj[x0] != alloc_obj[x1] else inner
            t[0] += 1
            t[1] += b == b"\0\0\0\0"
        report["stages"]["PADDING"] = {"zero_word_before_start": {"obj_boundary": bnd, "same_obj": inner},
                                       "admits": "never (a signature must differ between the two populations)",
                                       "note": "DS2: not distinguishable (about 5% everywhere)"}

    # ---- A8 MANUAL_EVIDENCE ----
    manual, conflicts = {}, []
    if ctx.inputs.get("manual_accepts"):
        for r in tsv(ctx.inputs["manual_accepts"]):
            if r["decision"] == "accept":
                manual[int(r["addr"], 16)] = r
    for x, r in sorted(manual.items()):
        if x in alloc_obj:
            if alloc_obj[x] != r["obj"]:
                conflicts.append("0x%08X" % x)
            continue
        link[x] = ("MANUAL_EVIDENCE", A.get("manual_basis", "config/residue_manual.tsv"), r["obj"], obj_lib.get(r["obj"], ""), None)
        alloc_obj[x] = r["obj"]
    report["stages"]["MANUAL_EVIDENCE"] = {"accepts": len(manual), "conflicts": conflicts}

    # ---- counterpart census (residue reasons; RETAIL_ONLY candidates) ----
    other_sigs = set()
    for label, (AP, ard) in aux.items():
        for p in AP["procs"]:
            n = int(p["size"] or 0)
            b = ard(int(p["proto_va"], 16), n) if n >= 4 and n % 4 == 0 else None
            if b and len(b) == n:
                other_sigs.add((n, ppc.masked_sig(b)))

    def has_counterpart(x):
        ext = nxt_of.get(x, x + 4) - x
        for n in range(max(4, ext - 12), ext + 1, 4):
            b = rrd(x, n)
            if b and len(b) == n:
                sg = (n, ppc.masked_sig(b))
                if sg in other_sigs or sg in co.index:
                    return True
        return False

    no_counterpart = {x for x in keys if x not in alloc_obj and not has_counterpart(x)}
    retail_only = no_counterpart if "RETAIL_ONLY_FOLDER" in exp else set()

    # ---- names, signatures, rows ----
    mangled_for = {}
    for addr in keys:
        r = rows.get(addr)
        ms = set()
        if r and r["start"]:
            for pa in r["pas"]:
                ms |= mangled_by_proto.get(pa, set())
        ms |= lane5.get(addr, set())
        if len(ms) == 1:
            mangled_for[addr] = next(iter(ms))
        elif len(ms) > 1:
            mangled_for[addr] = "AMBIGUOUS:%d" % len(ms)
    dm = demangle(ctx.inputs.get("undname"), list(mangled_for.values()))
    tp = ctx.tree

    out_rows = []
    for addr, sym in funcs:
        r = rows.get(addr)
        mg = mangled_for.get(addr, "")
        proto = dm.get(mg, "")
        if r:
            p = r["p"]
            cls, basis, obj, lib = r["cls"], r["basis"], p["obj"], p["lib"]
            src, line = r["src"], p["src_line"] if r["src"] == p["src_file"] else ""
            if not src:
                c = obj_src.get(obj)
                src = c.most_common(1)[0][0] if c else ""
                basis += ";src=" + ("obj_dominant" if src else "obj")
            if len(r["names"]) == 1:
                nstat, pname = ("PDB_NAME" if r["start"] else "PDB_PARENT+OFFSET"), r["names"][0]
            else:
                nstat, pname = "AMBIGUOUS:%d" % len(r["names"]), ""
            ct, npar, this = p["calltype"], p["nparams"], p["has_this"]
        elif addr in link:
            cls, basis, obj, lib, q = link[addr]
            if q and q["src_file"]:
                src, line = q["src_file"], ""
                basis += ";src=proto_proc"
            else:
                c = obj_src.get(obj)
                src, line = (c.most_common(1)[0][0] if c else ""), ""
                basis += ";src=" + ("obj_dominant" if src else "obj")
            nstat, pname, ct, npar, this = "NONE", "", "", "", ""
            if cls.startswith("PDB_BYTES") and q:
                nstat, pname = "PDB_NAME_BYTES", q["name"]
                ct, npar, this = q["calltype"], q["nparams"], q["has_this"]
                line = q["src_line"] if q["src_file"] == src else ""
        elif addr in inferred:
            obj = inferred[addr]
            cls, basis, lib = "INFERRED", "object_order", obj_lib.get(obj, "")
            c = obj_src.get(obj)
            src, line = (c.most_common(1)[0][0] if c else ""), ""
            nstat, pname, ct, npar, this = "NONE", "", "", "", ""
        elif addr in retail_only:
            cls, basis, obj, lib, src, line = "RETAIL_ONLY", "no_counterpart_in_any_pdb", "", "", "", ""
            nstat, pname, ct, npar, this = "NONE", "", "", "", ""
        else:
            cls, basis, obj, lib, src, line = "UNALLOCATED", "none", "", "", "", ""
            nstat, pname, ct, npar, this = "NONE", "", "", "", ""
        if cls in ("UNALLOCATED", "RETAIL_ONLY"):
            tree = "unattributed/%08X-%08X.cpp" % (addr & ~0xFFFF, (addr & ~0xFFFF) + 0xFFFF)
        else:
            tree = tp.tree_path(src or obj)
        ident = ctx.ident_for(pname, addr) if pname else ""
        out_rows.append(["0x%08X" % addr, sym, cls, basis, obj, lib, src, line, tree, nstat, pname, ident,
                         ct, npar, this, mg, proto])

    # ---- experimental: RETAIL_ONLY_FOLDER ----
    if "RETAIL_ONLY_FOLDER" in exp:
        def dir_of(t):
            return t.rsplit("/", 1)[0].split("/")

        def neighbours(i):
            lo = i - 1
            while lo >= 0 and out_rows[lo][2] in ("UNALLOCATED", "RETAIL_ONLY"):
                lo -= 1
            hi = i + 1
            while hi < len(out_rows) and out_rows[hi][2] in ("UNALLOCATED", "RETAIL_ONLY"):
                hi += 1
            return (lo, hi) if lo >= 0 and hi < len(out_rows) else None

        def shared(i, min_depth):
            nb = neighbours(i)
            if not nb:
                return None
            a_, b_ = dir_of(out_rows[nb[0]][8]), dir_of(out_rows[nb[1]][8])
            k = 0
            while k < min(len(a_), len(b_)) and a_[k] == b_[k]:
                k += 1
            return a_[:k] if k >= min_depth else None
        boundary = [i for i, r in enumerate(out_rows) if r[2] not in ("UNALLOCATED", "RETAIL_ONLY")
                    and neighbours(i) and out_rows[neighbours(i)[0]][4] != out_rows[neighbours(i)[1]][4]]
        depth_ok = None
        by_depth = {}
        for depth in range(2, 9):
            its = []
            for i in boundary:
                sd = shared(i, depth)
                if sd is not None:
                    its.append((int(out_rows[i][0], 16), dir_of(out_rows[i][8])[:len(sd)] == sd))
            by_depth[depth] = counts(its)
            if depth_ok is None and len(its) >= 100 and sum(ok for _, ok in its) / len(its) >= G.bar:
                depth_ok = (depth, its)
        g = G.judge("X_RETAIL_ONLY_FOLDER", depth_ok[1] if depth_ok else [], control=None,
                    require={"control_defined": False},
                    extra={"by_depth": by_depth, "note": "DS2: <= 0.974 at every depth, refused"})
        for r in out_rows:
            if r[2] == "RETAIL_ONLY":
                i = out_rows.index(r)
                sd = shared(i, depth_ok[0]) if (g["gate"] and depth_ok) else None
                if sd is None:
                    r[2], r[3] = "UNALLOCATED", "no_counterpart_in_any_pdb;no_enclosing_block"
                else:
                    r[8] = "/".join(sd) + "/_retail_only/%s.cpp" % r[0][2:]

    # ---- A9 UNDETERMINABLE ----
    und = {}
    if ctx.inputs.get("undeterminable"):
        for r in tsv(ctx.inputs["undeterminable"]):
            und[int(r["addr"], 16)] = r
    und_missing = sorted("0x%08X" % x for x, r in und.items()
                         if not r.get("refused_by", "").strip() or not r.get("evidence", "").strip())
    und_allocatable = []
    # An unallocated function that is NOT listed is held as UNDETERMINABLE with basis
    # PENDING_A8_BASIS: the naming pass's gated A8 (name -> obj) is its last chance. cli.py
    # refuses the run if any pending row is still unresolved after naming, or if A8 resolves a
    # listed row (the list is then stale). The final table invariant is unchanged: every
    # UNDETERMINABLE row is listed.
    pending = []
    for r in out_rows:
        x = int(r[0], 16)
        if x in und:
            if r[2] != "UNALLOCATED":
                und_allocatable.append(r[0])
            else:
                r[2], r[3], r[8] = "UNDETERMINABLE", A.get("undeterminable_basis", "config/undeterminable.tsv"), ""
        elif r[2] == "UNALLOCATED" and A.get("pending_a8", True):
            r[2], r[3], r[8] = "UNDETERMINABLE", PENDING_A8_BASIS, ""
            pending.append(x)
    report["pending_a8"] = len(pending)
    report["counterpart_census"] = {"aux_pdbs": sorted(aux), "no_counterpart_in_any_pdb": len(no_counterpart)}
    report["checks"] = {"every_registered_function_has_one_row": len(out_rows) == len(funcs) == len({r[0] for r in out_rows}),
                        "every_undeterminable_row_has_refusal_evidence": not und_missing,
                        "no_allocatable_function_in_undeterminable_list": not und_allocatable,
                        "manual_rows_agree_with_methods": not conflicts,
                        "zero_unallocated_outside_undeterminable_list": not any(r[2] == "UNALLOCATED" for r in out_rows)}
    report["no_counterpart"] = no_counterpart
    return out_rows, owners_of, report


PENDING_A8_BASIS = "pending:A8_NAME_TO_OBJ (not listed; resolved by the naming pass or the run is refused)"


def check_table(rows, registered_addrs, und_addrs):
    """The allocation table invariants (used for the self-shake): one row per registered
    address in order; every allocated row has an obj and a tree path; every UNDETERMINABLE row is
    listed and carries no folder; no UNALLOCATED row outside the list."""
    if [int(r[0], 16) for r in rows] != list(registered_addrs):
        return False
    for r in rows:
        x = int(r[0], 16)
        if r[2] == "UNDETERMINABLE":
            if x not in und_addrs or r[8]:
                return False
        elif r[2] == "UNALLOCATED":
            return False
        elif not r[4] or not r[8]:
            return False
    return True
