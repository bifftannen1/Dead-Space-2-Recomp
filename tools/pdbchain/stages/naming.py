"""Stage N: give a PDB procedure name to allocated functions that have a file but no name.

Methods in declared order (a later method sees earlier names as named); each is gated by the
shared engine on hidden known names of the same shape, with a control, before it admits:

  N1 offset start      x -> proto x-d (bracket delta, or each side's delta when they differ and
                       exactly one passes); a proc STARTS there in the allocated obj, unclaimed, one
                       distinct name, size within tolerance, opcode shape >= shape_min, no
                       contradicting named callee, and no other proc within +-k passes the same
  N2 ordered alignment between two named functions of one obj the unnamed retail functions and the
                       unclaimed procs are equal in number and pass N1's checks pairwise in order
  N3 callee multiset   >= 2 direct callees, all named; the callee-name multiset matches one proc
  (fixed point of N1 + N2)
  N4 refs tie-break    sibling ambiguity: the one candidate whose formed data addresses (mapped
                       through the evidence map) / strings / named callees agree (>= min_agree)
                       with no contradiction, every other candidate contradicting
  N5 local re-align    each side's nearest named obj-mate carries its own delta
  N6 folded names      N1 target carries several names (prototype ICF): primary + alias list
  N7 inexact opcode    best normalised-token similarity within +-3 procs; thresholds tuned on a
                       crc32 split, frozen, validated on the held-out split
Experimental: N8 (aux-PDB bodies as tie-break; DS2 refused at Wilson lower 0.934).
"""
from __future__ import annotations

import bisect, collections, csv, difflib, hashlib, io, random, zlib

from ..arch import ppc
from ..gates import wilson_lower as wilson
from ..util import tsv

NAMED = ("PDB_NAME", "PDB_NAME_BYTES")
# residue census: checks in the order N1 applies them; a side that fails later got further
CENSUS_ORDER = ["sibling_ambiguity", "callee_contradiction", "shape_mismatch", "size_mismatch",
                "target_other_obj", "folded_names_at_target", "target_claimed", "target_not_proc_start",
                "target_outside_obj"]
HEADER = ["addr", "method", "pdb_name", "ident", "proto_va", "obj_check", "evidence", "aliases"]


def run(cfg, ctx, alloc_rows, P, aux, evmap_path):
    C = cfg.get("naming", {})
    G = ctx.engine
    GATE = G.bar
    SHAPE_MIN = float(C.get("shape_min", 0.75))
    NEIGH = int(C.get("neighbour_k", 2))
    N4_MIN_AGREE = int(C.get("n4_min_agree", 2))
    HELD_SEED, HELD_W = int(C.get("heldout_seed", 20261007)), int(C.get("heldout_window", 2))
    N7_K = int(C.get("n7_window", 3))
    exp = set(C.get("experimental", []))
    methods = [m for m in C.get("methods", ["N1", "N2", "N3", "N4", "N5", "N6", "N7", "N9"])] + sorted(exp)
    rrd, prd = ctx.rrd, ctx.prd
    report = {"methods": {}}

    from .allocation import HEADER as AH
    rows = [dict(zip(AH, r)) for r in alloc_rows]
    keys = [int(r["addr"], 16) for r in rows]
    R = {int(r["addr"], 16): r for r in rows}
    nxt = dict(zip(keys, keys[1:]))
    size = {x: nxt.get(x, x + 4) - x for x in keys}
    size_registered = dict(size)
    EXTENTS = C.get("extents", "registered")  # "normalised": N1-N9 read the X0 extent table
    if EXTENTS == "normalised":
        from ..extents import for_ctx
        size = for_ctx(ctx, keys, size_registered, C.get("extent_normalisers", ["pdata", "tail_blr"]))[0]
    elif EXTENTS != "registered":
        raise SystemExit("REFUSED: naming.extents must be registered|normalised, got %r" % EXTENTS)
    report["extents"] = EXTENTS
    procs = collections.defaultdict(list)
    for r in P["procs"]:
        procs[int(r["proto_va"], 16)].append(r)
    pstarts = sorted(procs)
    contribs = sorted((int(c["proto_va"], 16), int(c["size"]), c["obj"]) for c in P["contribs"])
    cst = [c[0] for c in contribs]

    from ..common import size_ok

    def contrib_at(p):
        i = bisect.bisect_right(cst, p) - 1
        return contribs[i] if i >= 0 and contribs[i][0] <= p < contribs[i][0] + contribs[i][1] else None

    def pname(pa):
        ns = {p["name"] for p in procs.get(pa, ())}
        return next(iter(ns)) if len(ns) == 1 else None

    def pobj(pa):
        os_ = {p["obj"] for p in procs.get(pa, ())}
        return next(iter(os_)) if len(os_) == 1 else None

    def psize(pa):
        return max(int(p["size"] or 0) for p in procs[pa])

    anchor_rows, r2p, addr_map_rows = collections.defaultdict(set), collections.defaultdict(set), collections.defaultdict(set)
    for r in tsv(evmap_path):
        if r["retail_addr"] and r["proto_addr"] and r["bytes_class"] in ("EXACT", "DELTA"):
            addr_map_rows[int(r["proto_addr"], 16)].add(int(r["retail_addr"], 16))
        if (r["kind"] in ("proc", "public_code", "thunk") and r["retail_addr"] and r["proto_addr"]
                and r["bytes_class"] in ("EXACT", "DELTA")):
            ra, pa = int(r["retail_addr"], 16), int(r["proto_addr"], 16)
            anchor_rows[ra].add((ra - pa, r["contribution"].rsplit("@", 1)[0]))
            if r["kind"] == "proc" and pa in procs:
                r2p[ra].add(pa)
    anchors = {ra: next(iter(v)) for ra, v in anchor_rows.items() if len(v) == 1}
    ka = sorted(anchors)

    truth = {}
    for x, r in R.items():
        if r["name_status"] in NAMED:
            pas = [pa for pa in r2p.get(x, ()) if pname(pa) == r["pdb_name"]]
            if len(pas) == 1:
                truth[x] = pas[0]
    claimed = set()
    for x, r in R.items():
        if r["name_status"] in NAMED or r["name_status"].startswith("AMBIGUOUS"):
            claimed |= r2p.get(x, set())
    claimed |= set(truth.values())

    rcalls = {x: ppc.bl_targets(rrd(x, size[x]), x) for x in keys}
    pcache = {}

    def pcalls(pa):
        if pa not in pcache:
            pcache[pa] = ppc.bl_targets(prd(pa, psize(pa)), pa)
        return pcache[pa]

    name_of = {x: R[x]["pdb_name"] for x in keys if R[x]["name_status"] in NAMED}
    pa_of = dict(truth)

    def callee_check(x, pa, hide=None):
        rc, pc = rcalls[x], pcalls(pa)
        rn = [name_of.get(t) if t != hide else None for t in rc]
        pn = [pname(t) if t in procs else None for t in pc]
        ag = co = 0
        if len(rn) == len(pn):
            for u, v in zip(rn, pn):
                if u is not None:
                    if v == u:
                        ag += 1
                    else:
                        co += 1
        else:
            ps = set(pn)
            for u in rn:
                if u is not None:
                    if u in ps:
                        ag += 1
                    else:
                        co += 1
        return ag, co

    scache = {}

    def shape(x, pa):
        k = (x, pa)
        if k not in scache:
            rb, pb = rrd(x, psize(pa)), prd(pa, psize(pa))
            scache[k] = 0.0 if not rb or not pb else \
                difflib.SequenceMatcher(None, ppc.shape_ops(rb), ppc.shape_ops(pb), autojunk=False).ratio()
        return scache[k]

    def bracket(x):
        i = bisect.bisect_left(ka, x)
        lo, hi = i - 1, i + 1 if i < len(ka) and ka[i] == x else i
        if lo < 0 or hi >= len(ka):
            return None, None, None
        return anchors[ka[lo]][0], anchors[ka[hi]][0], (ka[lo], ka[hi])

    def accept(x, pa, obj, taken, hide=None):
        if pa not in procs or pa in taken:
            return False
        if pname(pa) is None or pobj(pa) != obj:
            return False
        if not size_ok(size[x], psize(pa)):
            return False
        if shape(x, pa) < SHAPE_MIN:
            return False
        return callee_check(x, pa, hide)[1] == 0

    obj_procs = collections.defaultdict(list)
    for pa in pstarts:
        o = pobj(pa)
        if o:
            obj_procs[o].append(pa)

    def sibling_passes(x, p, obj, taken, hide, k=NEIGH):
        op = obj_procs.get(obj, [])
        i = bisect.bisect_left(op, p)
        for j in range(max(0, i - k), min(len(op), i + k + 1)):
            q = op[j]
            if q != p and accept(x, q, obj, taken, hide):
                return True
        return False

    def n1(x, obj, taken, hide=None, shift=0):
        dl, dr, _ = bracket(x)
        ds = []
        if dl is not None and dl == dr:
            ds = [dl]
        elif dl is not None:
            ds = [dl, dr]
        hits = set()
        for d in ds:
            p = x - d
            if shift:
                j = bisect.bisect_right(pstarts, p) + shift - 1
                if not 0 <= j < len(pstarts):
                    continue
                p = pstarts[j]
                if p in {x - e for e in ds}:
                    continue
            if accept(x, p, obj, taken, hide) and not sibling_passes(x, p, obj, taken, hide):
                hits.add(p)
        return (next(iter(hits)), "both" if len(ds) == 1 else "one") if len(hits) == 1 else (None, None)

    new = {}
    tkeys = sorted(truth)
    report["truth_named_functions"] = len(truth)

    def gates_ok(m):
        return report["methods"].get(m, {}).get("gate", False)

    # development cache (--dev-cache): reuse N1-N9 results when naming.py, its inputs and the
    # config are unchanged; the N blocks are then skipped (never in a real run)
    from .. import devcache as DEVC
    _dk = getattr(ctx, "dev_cache_key", None)
    _hit = DEVC.load(ctx.work, "naming", _dk) if _dk else None
    _greps0 = set(G.report)
    ctx.dev_cache_hit = bool(_hit)
    if _hit:
        new.update(_hit["new"]); claimed.clear(); claimed.update(_hit["claimed"])
        name_of.clear(); name_of.update(_hit["name_of"]); pa_of.clear(); pa_of.update(_hit["pa_of"])
        report.update(_hit["report"]); G.report.update(_hit["greps"])
        methods = []
    # ---------------- N1 ----------------
    if "N1" in methods:
        items, cn, ca, sub = [], 0, 0, collections.defaultdict(list)
        for x in tkeys:
            pa = truth[x]
            taken = claimed - {pa}
            obj = R[x]["obj"]
            saved = name_of.pop(x)
            try:
                got = n1(x, obj, taken, hide=x)
                cg = n1(x, obj, taken, hide=x, shift=1)
            finally:
                name_of[x] = saved
            if got[0] is not None:
                items.append((x, got[0] == pa))
                diff = rrd(x, psize(pa)) != prd(pa, psize(pa))
                for key in ["bytes_differ" if diff else "bytes_equal", "kind_%s" % got[1]]:
                    sub[key].append(got[0] == pa)
            cn += 1
            ca += cg[0] is not None
        req = {"subset_%s_ge_bar" % k: sum(v) / len(v) >= GATE for k, v in sorted(sub.items()) if len(v) >= 20}
        g = G.judge("N1_OFFSET_START", items, control=(cn, ca), require=req,
                    extra={"population": len(tkeys), "control_kind": "proto target shifted to the next proc start",
                           "subsets": {k: {"decided": len(v), "correct": sum(v)} for k, v in sorted(sub.items())}})
        report["methods"]["N1"] = g
        if g["gate"]:
            taken = set(claimed)
            for x in keys:
                r = R[x]
                if r["name_status"] != "NONE" or not r["obj"]:
                    continue
                pa, kind = n1(x, r["obj"], taken)
                if pa is not None:
                    new[x] = (pa, "N1", "delta=%d;%s;proto=0x%08X" % (x - pa, kind, pa))
            cnt = collections.Counter(v[0] for v in new.values())
            for x in [x for x, v in new.items() if cnt[v[0]] > 1]:
                del new[x]
            for x, v in new.items():
                claimed.add(v[0]); name_of[x] = pname(v[0]); pa_of[x] = v[0]
            g["admitted_first_round"] = sum(v[1] == "N1" for v in new.values())
        ctx.log("N1 gate=%s admitted=%s" % (g["gate"], g.get("admitted_first_round")))

    # ---------------- N2 ----------------
    def n2_runs(taken, hide=None):
        by_obj = collections.defaultdict(list)
        for x in keys:
            o = R[x]["obj"]
            if o:
                by_obj[o].append(x)
        out = {}
        for o, xs in by_obj.items():
            named = [x for x in xs if x in pa_of and x != hide and pa_of[x] in procs]
            for xl, xr in zip(named, named[1:]):
                pl, pr = pa_of[xl], pa_of[xr]
                if pr <= pl:
                    continue
                run_ = [x for x in xs if xl < x < xr and (x not in name_of or x == hide)]
                if not run_:
                    continue
                op = obj_procs[o]
                cand = [p for p in op[bisect.bisect_right(op, pl):bisect.bisect_left(op, pr)] if p not in taken]
                if len(cand) != len(run_):
                    continue
                if all(accept(x, p, o, taken - set(cand), hide) for x, p in zip(run_, cand)):
                    for x, p in zip(run_, cand):
                        out[x] = (p, "between=0x%08X..0x%08X;run=%d" % (xl, xr, len(run_)))
        return out

    if "N2" in methods:
        rng = random.Random(13)
        sample = rng.sample(tkeys, min(6000, len(tkeys)))
        by_obj = collections.defaultdict(list)
        for x in keys:
            if R[x]["obj"]:
                by_obj[R[x]["obj"]].append(x)
        items, sh_n, sh_a = [], 0, 0

        def gap_assign(x, taken, H, shift=0):
            o = R[x]["obj"]
            xs = by_obj[o]
            i = bisect.bisect_left(xs, x)
            l = next((y for y in reversed(xs[:i]) if y in pa_of and y not in H), None)
            r_ = next((y for y in xs[i + 1:] if y in pa_of and y not in H), None)
            if l is None or r_ is None:
                return {}
            pl, pr = pa_of[l], pa_of[r_]
            if pr <= pl:
                return {}
            run_ = [y for y in xs if l < y < r_ and (y not in name_of or y in H)]
            op = obj_procs[o]
            j0, j1 = bisect.bisect_right(op, pl), bisect.bisect_left(op, pr)
            cand = [p for p in op[j0 + shift:j1 + shift] if p not in taken]
            if not run_ or len(cand) != len(run_):
                return {}
            if not all(accept(y, p, o, taken - set(cand), x) for y, p in zip(run_, cand)):
                return {}
            return {y: p for y, p in zip(run_, cand) if y in H}

        blocks = collections.Counter()
        for x in sample:
            o = R[x]["obj"]
            xs = by_obj[o]
            i = bisect.bisect_left(xs, x)
            k = rng.choice((1, 2, 3, 5, 8))
            H = [y for y in xs[i:i + k] if y in truth]
            if not H or H[0] != x:
                continue
            blocks[len(H)] += 1
            taken = claimed - {truth[y] for y in H}
            saved = {y: name_of.pop(y) for y in H}
            savedp = {y: pa_of.pop(y) for y in H}
            try:
                got = gap_assign(x, taken, set(H))
                cg = gap_assign(x, taken, set(H), shift=1)
            finally:
                name_of.update(saved); pa_of.update(savedp)
            for y, p in got.items():
                items.append((y, p == truth[y]))
            sh_n += len(H); sh_a += sum(1 for y, p in cg.items() if p != truth[y])
        g = G.judge("N2_ORDERED_ALIGNMENT", items, control=(sh_n, sh_a),
                    extra={"sample": len(sample), "hidden_block_sizes": dict(sorted(blocks.items())),
                           "control_kind": "proc window shifted by one"})
        report["methods"]["N2"] = g
        if g["gate"]:
            got = n2_runs(claimed)
            cnt = collections.Counter(p for p, _ in got.values())
            k = 0
            for x, (p, ev) in got.items():
                if cnt[p] == 1 and x not in new and R[x]["name_status"] == "NONE":
                    new[x] = (p, "N2", ev + ";proto=0x%08X" % p)
                    claimed.add(p); name_of[x] = pname(p); pa_of[x] = p
                    k += 1
            g["admitted_first_round"] = k
        ctx.log("N2 gate=%s" % g["gate"])

    # ---------------- N3 ----------------
    def n3(x, obj, taken, hide=None):
        rc = rcalls[x]
        if len(rc) < 2:
            return (None, None)
        ns = [name_of.get(t) if t != hide else None for t in rc]
        if any(v is None for v in ns):
            return (None, None)
        s = tuple(sorted(ns))
        hits = []
        for p in obj_procs.get(obj, []):
            if p in taken or pname(p) is None:
                continue
            pn = [pname(t) if t in procs else None for t in pcalls(p)]
            if tuple(sorted(v for v in pn if v is not None)) == s and len(pn) == len(ns):
                hits.append(p)
        if len(hits) == 1 and size_ok(size[x], psize(hits[0])):
            return (hits[0], "callees=%d" % len(ns))
        return (None, None)

    if "N3" in methods:
        rng = random.Random(7)
        sample = rng.sample(tkeys, min(4000, len(tkeys)))
        items = []
        for x in sample:
            pa = truth[x]
            saved = name_of.pop(x)
            try:
                got = n3(x, R[x]["obj"], claimed - {pa}, hide=x)
            finally:
                name_of[x] = saved
            if got[0] is not None:
                items.append((x, got[0] == pa))
        objs = sorted(obj_procs)
        ca = cn = 0
        for x in sample[:1500]:
            o = rng.choice(objs)
            if o == R[x]["obj"]:
                continue
            saved = name_of.pop(x)
            try:
                got = n3(x, o, claimed - {truth[x]}, hide=x)
            finally:
                name_of[x] = saved
            cn += 1; ca += got[0] is not None
        g = G.judge("N3_CALLEE_MULTISET", items, control=(cn, ca),
                    extra={"sample": len(sample), "control_kind": "multiset searched in a random other obj"})
        report["methods"]["N3"] = g
        if g["gate"]:
            got = {}
            for x in keys:
                r = R[x]
                if r["name_status"] == "NONE" and x not in new and r["obj"]:
                    p, ev = n3(x, r["obj"], claimed)
                    if p is not None:
                        got[x] = (p, ev)
            cnt = collections.Counter(p for p, _ in got.values())
            k = 0
            for x, (p, ev) in got.items():
                if cnt[p] == 1:
                    new[x] = (p, "N3", ev + ";proto=0x%08X" % p)
                    claimed.add(p); name_of[x] = pname(p); pa_of[x] = p
                    k += 1
            g["admitted_first_round"] = k
        ctx.log("N3 gate=%s" % g["gate"])

    # ---------------- fixed point ----------------
    rounds = []
    for rnd in (range(2, 12) if not _hit else ()):  # a cache hit already holds the fixed point
        add = {}
        if gates_ok("N1"):
            got = {}
            for x in keys:
                r = R[x]
                if r["name_status"] == "NONE" and x not in new and r["obj"]:
                    pa, kind = n1(x, r["obj"], claimed)
                    if pa is not None:
                        got[x] = (pa, "N1", "delta=%d;%s;proto=0x%08X;round=%d" % (x - pa, kind, pa, rnd))
            cnt = collections.Counter(v[0] for v in got.values())
            add.update({x: v for x, v in got.items() if cnt[v[0]] == 1})
        for x, v in add.items():
            new[x] = v; claimed.add(v[0]); name_of[x] = pname(v[0]); pa_of[x] = v[0]
        k2 = 0
        if gates_ok("N2"):
            got = n2_runs(claimed)
            cnt = collections.Counter(p for p, _ in got.values())
            for x, (p, ev) in got.items():
                if cnt[p] == 1 and x not in new and R[x]["name_status"] == "NONE" and p not in claimed:
                    new[x] = (p, "N2", ev + ";proto=0x%08X;round=%d" % (p, rnd))
                    claimed.add(p); name_of[x] = pname(p); pa_of[x] = p
                    k2 += 1
        rounds.append({"round": rnd, "N1": len(add), "N2": k2})
        if len(add) + k2 == 0:
            break
    if not _hit:
        report["fixed_point_rounds"] = rounds

    # ================= second pass =================
    addr_map = {k: next(iter(v)) for k, v in addr_map_rows.items() if len(v) == 1}
    addr_keys = sorted(addr_map)

    def text_at(rd_, va):
        b = rd_(va, 64)
        if not b:
            return None
        s = b.split(b"\0", 1)[0]
        if 4 <= len(s) < 64 and all(32 <= c < 127 for c in s):
            return s.decode("ascii")
        return None

    rkcache, pkcache = {}, {}

    def rkeys(x):
        if x not in rkcache:
            ks = set()
            for v in ppc.formed(rrd(x, size[x])):
                ks.add(("a", v))
                t = text_at(rrd, v)
                if t:
                    ks.add(("s", t))
            rkcache[x] = ks
        return rkcache[x]

    def pkeys(pa):
        if pa not in pkcache:
            ks = set()
            for v in ppc.formed(prd(pa, psize(pa))):
                j = bisect.bisect_right(addr_keys, v) - 1
                if j >= 0 and v - addr_keys[j] < 0x100:
                    ks.add(("a", (addr_map[addr_keys[j]] + v - addr_keys[j]) & 0xFFFFFFFF))
                t = text_at(prd, v)
                if t:
                    ks.add(("s", t))
            pkcache[pa] = ks
        return pkcache[pa]

    def refscore(x, q, hide, kx=None):
        kr = rkeys(x) if kx is None else kx
        kp = pkeys(q)
        ag, co = callee_check(x, q, hide)
        return len(kr & kp) + ag, len(kp - kr) + co

    def n1_targets(x):
        dl, dr, _ = bracket(x)
        if dl is None:
            return []
        return [x - dl] if dl == dr else [x - dl, x - dr]

    def n4_decide(x, Cs, obj, taken, hide=None, kx=None):
        sc = {q: refscore(x, q, hide, kx) for q in Cs}
        good = [q for q, (ag, co) in sc.items() if co == 0 and ag >= N4_MIN_AGREE and accept(x, q, obj, taken, hide)]
        if len(good) == 1 and all(sc[q][1] > 0 for q in Cs if q != good[0]):
            return good[0]
        return None

    def n4(x, obj, taken, hide=None, kx=None):
        op = obj_procs.get(obj, [])
        Cs = set()
        for p in n1_targets(x):
            i = bisect.bisect_left(op, p)
            for j in range(max(0, i - NEIGH), min(len(op), i + NEIGH + 1)):
                q = op[j]
                if q not in taken and pname(q) is not None and size_ok(size[x], psize(q)):
                    Cs.add(q)
        if len(Cs) < 2:
            return None, len(Cs)
        return n4_decide(x, Cs, obj, taken, hide, kx), len(Cs)

    def hide_truth(x):
        return name_of.pop(x, None), pa_of.pop(x, None)

    def unhide(x, saved):
        if saved[0] is not None:
            name_of[x] = saved[0]
        if saved[1] is not None:
            pa_of[x] = saved[1]

    def admit(got, method):
        cnt = collections.Counter(v[0] for v in got.values())
        k = 0
        for x, (p, ev) in sorted(got.items()):
            if cnt[p] == 1 and p not in claimed and x not in new and R[x]["name_status"] == "NONE":
                new[x] = (p, method, ev + ";proto=0x%08X" % p)
                claimed.add(p); pa_of[x] = p
                if pname(p):
                    name_of[x] = pname(p)
                k += 1
        return k

    tuned_ids = []
    if "N4" in methods:
        rng = random.Random(4)
        items, cn, ca, pop = [], 0, 0, 0
        for x in tkeys:
            pa = truth[x]
            s_ = hide_truth(x)
            try:
                taken = claimed - {pa}
                got, nc = n4(x, R[x]["obj"], taken, hide=x)
                if nc >= 2:
                    pop += 1
                    tuned_ids.append(x)
                    if got is not None:
                        items.append((x, got == pa))
                    y = rng.choice(tkeys)
                    cg, _ = n4(x, R[x]["obj"], taken, hide=x, kx=rkeys(y) if y != x else set())
                    cn += 1; ca += cg is not None and cg != pa
            finally:
                unhide(x, s_)
        # near-shape held-out of the frozen rule (tuning IDs excluded)
        tuned = set(tuned_ids)
        rngh = random.Random(HELD_SEED)
        pool = [x for x in tkeys if x not in tuned]
        rngh.shuffle(pool)
        held = []
        for x in pool:
            pa = truth[x]
            o = R[x]["obj"]
            op = obj_procs.get(o, [])
            i = bisect.bisect_left(op, pa)
            if i >= len(op) or op[i] != pa:
                continue
            taken = claimed - {pa}
            Cs = {op[j] for j in range(max(0, i - HELD_W), min(len(op), i + HELD_W + 1))
                  if op[j] == pa or (op[j] not in taken and pname(op[j]) is not None)}
            if len(Cs) < 2:
                continue
            s_ = hide_truth(x)
            try:
                got = n4_decide(x, Cs, o, taken, hide=x)
            finally:
                unhide(x, s_)
            if got is not None:
                held.append((x, got == pa))
        g = G.judge("N4_REFS_TIEBREAK", items, control=(cn, ca), heldout_items=held,
                    extra={"population_sibling_ambiguous": pop, "min_agree": N4_MIN_AGREE,
                           "heldout": {"seed": HELD_SEED, "window": HELD_W, "excluded_tuning_ids": len(tuned),
                                       "tuning_ids_sha256": hashlib.sha256(",".join("%08X" % t for t in sorted(tuned)).encode()).hexdigest()},
                           "note": "min_agree=2 chosen on tune after min_agree=1 measured 92/93 (DS2)",
                           "control_kind": "refs/strings of a random other function: wrong pick accepted"})
        report["methods"]["N4"] = g
        if g["gate"]:
            got = {}
            for x in keys:
                r = R[x]
                if r["name_status"] == "NONE" and x not in new and r["obj"]:
                    p, nc = n4(x, r["obj"], claimed)
                    if p is not None:
                        got[x] = (p, "refs_tiebreak;candidates=%d" % nc)
            g["admitted"] = admit(got, "N4")
        ctx.log("N4 gate=%s" % g["gate"])

    by_obj_all = collections.defaultdict(list)
    for x in keys:
        if R[x]["obj"]:
            by_obj_all[R[x]["obj"]].append(x)

    def n5(x, obj, taken, hide=None, shift=0):
        xs = by_obj_all[obj]
        i = bisect.bisect_left(xs, x)
        l = next((y for y in reversed(xs[:i]) if y in pa_of and y != hide), None)
        r_ = next((y for y in xs[i + 1:] if y in pa_of and y != hide), None)
        ts = set()
        if l is not None:
            ts.add(pa_of[l] + (x - l))
        if r_ is not None:
            ts.add(pa_of[r_] - (r_ - x))
        hits = set()
        op = obj_procs.get(obj, [])
        for p in ts:
            if shift:
                j = bisect.bisect_right(op, p) + shift - 1
                if not 0 <= j < len(op) or op[j] in ts:
                    continue
                p = op[j]
            if accept(x, p, obj, taken, hide) and not sibling_passes(x, p, obj, taken, hide):
                hits.add(p)
        return (next(iter(hits)), len(ts)) if len(hits) == 1 else (None, len(ts))

    if "N5" in methods:
        items, cn, ca, sub = [], 0, 0, []
        for x in tkeys:
            pa = truth[x]
            s_ = hide_truth(x)
            try:
                taken = claimed - {pa}
                got, _ = n5(x, R[x]["obj"], taken, hide=x)
                cg, _ = n5(x, R[x]["obj"], taken, hide=x, shift=1)
            finally:
                unhide(x, s_)
            if got is not None:
                items.append((x, got == pa))
                if pa not in n1_targets(x):
                    sub.append(got == pa)
            cn += 1; ca += cg is not None and cg != pa
        g = G.judge("N5_LOCAL_REALIGN", items, control=(cn, ca),
                    require={"same_shape_bracket_misses_start_n_ge_20_and_ge_bar": len(sub) >= 20 and sum(sub) / len(sub) >= GATE},
                    extra={"bracket_misses_start": {"decided": len(sub), "correct": sum(sub)},
                           "control_kind": "each target shifted to the next proc of the obj"})
        report["methods"]["N5"] = g
        if g["gate"]:
            got = {}
            for x in keys:
                r = R[x]
                if r["name_status"] == "NONE" and x not in new and r["obj"]:
                    p, _ = n5(x, r["obj"], claimed)
                    if p is not None:
                        got[x] = (p, "local_realign")
            g["admitted"] = admit(got, "N5")
        ctx.log("N5 gate=%s" % g["gate"])

    def names_at(pa):
        return sorted({p["name"] for p in procs.get(pa, ())})

    def accept_folded(x, pa, obj, taken, hide=None):
        if pa not in procs or pa in taken or len(names_at(pa)) < 2:
            return False
        if obj not in {p["obj"] for p in procs[pa]}:
            return False
        return size_ok(size[x], psize(pa)) and shape(x, pa) >= SHAPE_MIN and callee_check(x, pa, hide)[1] == 0

    def folded_sibling(x, p, obj, taken, hide):
        i = bisect.bisect_left(pstarts, p)
        for j in range(max(0, i - NEIGH), min(len(pstarts), i + NEIGH + 1)):
            q = pstarts[j]
            if q != p and (accept(x, q, obj, taken, hide) or accept_folded(x, q, obj, taken, hide)):
                return True
        return False

    def n6(x, obj, taken, hide=None, shift=0):
        hits = set()
        ts = n1_targets(x)
        for p in ts:
            if shift:
                j = bisect.bisect_right(pstarts, p) + shift - 1
                if not 0 <= j < len(pstarts) or pstarts[j] in ts:
                    continue
                p = pstarts[j]
            if accept_folded(x, p, obj, taken, hide) and not folded_sibling(x, p, obj, taken, hide):
                hits.add(p)
        return next(iter(hits)) if len(hits) == 1 else None

    if "N6" in methods:
        items, cn, ca = [], 0, 0
        amb = [x for x in keys if R[x]["name_status"].startswith("AMBIGUOUS") and len(r2p.get(x, ())) == 1]
        for x in amb:
            pa = next(iter(r2p[x]))
            taken = claimed - {pa}
            got = n6(x, R[x]["obj"], taken, hide=x)
            cg = n6(x, R[x]["obj"], taken, hide=x, shift=1)
            if got is not None:
                items.append((x, got == pa))
            cn += 1; ca += cg is not None and cg != pa
        tn = tok = 0
        for x in tkeys:
            s_ = hide_truth(x)
            try:
                for p in n1_targets(x):
                    if p in procs and size_ok(size[x], psize(p)) and shape(x, p) >= SHAPE_MIN:
                        tn += 1; tok += R[x]["pdb_name"] in names_at(p)
                        # the N6 claim on a hidden known answer: x -> its offset target p, claim =
                        # every name at p; correct when the true name is in it (as N9 measures)
                        items.append((x, R[x]["pdb_name"] in names_at(p)))
                        break
            finally:
                unhide(x, s_)
        g = G.judge("N6_FOLDED_ALIASES", items, control=(cn, ca),
                    require={"n_ge_20": len(items) >= 20, "true_name_in_alias_list_ge_bar": bool(tn) and tok / tn >= GATE},
                    extra={"population_folded": len(amb), "true_name_in_alias_list_unique_truth": {"decided": tn, "correct": tok},
                           "claim": "alias list (primary = first sorted name)",
                           "control_kind": "target shifted to the next proc start"})
        report["methods"]["N6"] = g
        if g["gate"]:
            got = {}
            for x in keys:
                r = R[x]
                if r["name_status"] == "NONE" and x not in new and r["obj"]:
                    p = n6(x, r["obj"], claimed)
                    if p is not None:
                        got[x] = (p, "folded;aliases=%d" % len(names_at(p)))
            g["admitted"] = admit(got, "N6")
        ctx.log("N6 gate=%s" % g["gate"])

    # ================= third pass: N7 (tuned on a split, frozen, held out) =================
    def split_of(x):
        return "tune" if zlib.crc32(b"N78:%08X" % x) % 2 == 0 else "held"

    rtok, ptok, simc = {}, {}, {}

    def rt(x):
        if x not in rtok:
            rtok[x] = ppc.tokens(rrd(x, size[x]))
        return rtok[x]

    def pt(pa):
        if pa not in ptok:
            ptok[pa] = ppc.tokens(prd(pa, psize(pa)))
        return ptok[pa]

    def sim(x, pa):
        k = (x, pa)
        if k not in simc:
            a_, b_ = rt(x), pt(pa)
            simc[k] = difflib.SequenceMatcher(None, a_, b_, autojunk=False).ratio() if a_ and b_ else 0.0
        return simc[k]

    def n7_decide(x, Cs, hide, T, M, Rq=0):
        sc = sorted(((sim(x, q), q) for q in Cs if callee_check(x, q, hide)[1] == 0), reverse=True)
        if not sc:
            return None
        best = sc[0]
        second = sc[1][0] if len(sc) > 1 else 0.0
        if best[0] >= T and best[0] - second >= M:
            if Rq:
                ag, co = refscore(x, best[1], hide)
                if co or ag < Rq:
                    return None
            return best[1]
        return None

    def window(obj, centre, k, taken, lift=False):
        op = obj_procs.get(obj, [])
        i = bisect.bisect_left(op, centre)
        return {op[j] for j in range(max(0, i - k), min(len(op), i + k + 1))
                if pname(op[j]) is not None and (lift or op[j] not in taken)}

    def eval7(xs, T, M, shift=0, Rq=0):
        its = []
        for x in xs:
            pa = truth[x]
            o = R[x]["obj"]
            op = obj_procs.get(o, [])
            if pa not in procs or pa not in op:
                continue
            centre = pa
            if shift:
                i = op.index(pa) + shift
                if i >= len(op):
                    continue
                centre = op[i]
            Cs = window(o, centre, N7_K, set(), lift=True)
            if shift:
                Cs.discard(pa)
            if len(Cs) < 2 and not shift:
                continue
            s_ = hide_truth(x)
            try:
                got = n7_decide(x, Cs, x, T, M, Rq)
            finally:
                unhide(x, s_)
            if got is not None:
                its.append((x, got == pa))
        return its

    if "N7" in methods:
        rng7 = random.Random(HELD_SEED + 7)
        tune = [x for x in tkeys if split_of(x) == "tune"]
        heldk = [x for x in tkeys if split_of(x) == "held"]
        tune_s = rng7.sample(tune, min(5000, len(tune)))
        held_s = rng7.sample(heldk, min(8000, len(heldk)))
        grid, best_items = [], {}
        for Rq in (0, 1, 2):
            for T in (0.6, 0.8, 0.9, 0.95):
                for M in (0.05, 0.1, 0.2, 0.3):
                    its = eval7(tune_s, T, M, Rq=Rq)
                    c_ = len(eval7(tune_s, T, M, shift=2 * N7_K + 1, Rq=Rq))
                    ok_ = sum(1 for _, o in its if o)
                    grid.append({"T": T, "M": M, "min_ref_agree": Rq, "decided": len(its), "correct": ok_,
                                 "wilson95_lower": round(wilson(ok_, len(its)), 5), "control_rate": round(c_ / len(tune_s), 5)})
                    best_items[(T, M, Rq)] = its
        okg = [g_ for g_ in grid if g_["wilson95_lower"] >= GATE and g_["control_rate"] < G.control_bar / 2]
        pick = max(okg, key=lambda g_: (g_["decided"], g_["T"], g_["M"])) if okg else None
        extra = {"tuning_grid": grid, "frozen": pick, "window": N7_K, "split": "crc32('N78:%08X') % 2",
                 "candidate_shape": "true proc plus every single-name proc of the obj within +-k, claims lifted, no size filter"}
        if pick:
            T7, M7, R7 = pick["T"], pick["M"], pick["min_ref_agree"]
            held_items = eval7(held_s, T7, M7, Rq=R7)
            cn_ = len(eval7(held_s, T7, M7, shift=2 * N7_K + 1, Rq=R7))
            g = G.judge("N7_INEXACT_OPCODE", best_items[(T7, M7, R7)], control=(len(held_s), cn_),
                        heldout_items=held_items, extra=extra | {"control_kind": "held-out: window moved off the truth"})
        else:
            g = G.judge("N7_INEXACT_OPCODE", [], control=None, require={"tuning_found_setting": False}, extra=extra)
        report["methods"]["N7"] = g
        if g["gate"]:
            got = {}
            for x in keys:
                r = R[x]
                if r["name_status"] != "NONE" or x in new or not r["obj"]:
                    continue
                ts = n1_targets(x)
                if not ts:
                    continue
                Cs = set()
                for t in ts:
                    Cs |= window(r["obj"], t, N7_K, claimed)
                if not Cs:
                    continue
                p = n7_decide(x, Cs, None, T7, M7, R7)
                if p is not None:
                    got[x] = (p, "inexact;sim=%.3f;candidates=%d" % (sim(x, p), len(Cs)))
            g["admitted"] = admit(got, "N7")
        ctx.log("N7 gate=%s" % g["gate"])

    # ---- experimental N8: aux-PDB bodies as tie-break for the sibling ambiguity ----
    if "N8" in exp and aux:
        other = {}
        for label, (AP, ard) in aux.items():
            byname = collections.defaultdict(set)
            for r in AP["procs"]:
                if int(r["size"] or 0) >= 4:
                    byname[r["name"]].add((int(r["proto_va"], 16), int(r["size"])))
            other[label] = (byname, ard)
        otok = {}

        def corrob(x, nm):
            best = 0.0
            for b, (byname, rd_) in other.items():
                s_ = byname.get(nm, ())
                if len(s_) != 1:
                    continue
                va, sz = next(iter(s_))
                if (b, va) not in otok:
                    otok[(b, va)] = ppc.tokens(rd_(va, sz))
                t_ = otok[(b, va)]
                if t_ and rt(x):
                    best = max(best, difflib.SequenceMatcher(None, rt(x), t_, autojunk=False).ratio())
            return best

        def n8_decide(x, Cs, T, M):
            sc = sorted(((corrob(x, pname(q)), q) for q in Cs), reverse=True)
            if len(sc) < 2:
                return None
            return sc[0][1] if sc[0][0] >= T and sc[0][0] - sc[1][0] >= M else None

        def amb_set(x, o, taken, hide, lift):
            Cs = set()
            for t in n1_targets(x):
                for q in window(o, t, NEIGH, taken, lift=lift):
                    if accept(x, q, o, set() if lift else taken, hide):
                        Cs.add(q)
            return Cs

        def eval8(xs, T, M):
            its = []
            for x in xs:
                pa = truth[x]
                s_ = hide_truth(x)
                try:
                    Cs = amb_set(x, R[x]["obj"], set(), x, True)
                    got = n8_decide(x, Cs, T, M) if pa in Cs and len(Cs) >= 2 else None
                finally:
                    unhide(x, s_)
                if got is not None:
                    its.append((x, got == pa))
            return its
        rng8 = random.Random(HELD_SEED + 8)
        tune = [x for x in tkeys if split_of(x) == "tune"]
        tune_s = rng8.sample(tune, min(8000, len(tune)))
        ts_set = set(tune_s)
        held_s = [x for x in tkeys if split_of(x) == "held" and x not in ts_set]
        grid, gi = [], {}
        for T in (0.8, 0.9, 0.95, 1.0):
            for M in (0.02, 0.05, 0.1, 0.2):
                its = eval8(tune_s, T, M)
                ok_ = sum(1 for _, o in its if o)
                grid.append({"T": T, "M": M, "decided": len(its), "correct": ok_, "wilson95_lower": round(wilson(ok_, len(its)), 5)})
                gi[(T, M)] = its
        okg = [g_ for g_ in grid if g_["wilson95_lower"] >= GATE]
        pick = max(okg, key=lambda g_: (g_["decided"], g_["T"], g_["M"])) if okg else None
        if pick:
            held_items = eval8(held_s, pick["T"], pick["M"])
            g = G.judge("X_N8_AUX_TIEBREAK", gi[(pick["T"], pick["M"])], control=None, heldout_items=held_items,
                        require={"control_defined": False}, extra={"tuning_grid": grid, "frozen": pick})
        else:
            g = G.judge("X_N8_AUX_TIEBREAK", [], control=None, require={"tuning_found_setting": False},
                        extra={"tuning_grid": grid, "note": "DS2: tuning best 148/152 (Wilson lower 0.934), refused"})
        report["methods"]["N8"] = g

    # ---- N9: the allocation's AMBIGUOUS starts (evidence-map start carrying several folded names) ----
    # Claim: the alias list = every PDB name at the evidence-map proto start (primary = first sorted).
    # LOO on hidden unique truth: hide the row's name, take its bracket target that passes size+shape,
    # correct = the true name is in that start's alias list. Control: the same with the next proc start.
    if "N9" in methods:
        items, cn, ca = [], 0, 0
        for x in tkeys:
            s_ = hide_truth(x)
            try:
                for p in n1_targets(x):
                    if p in procs and size_ok(size[x], psize(p)) and shape(x, p) >= SHAPE_MIN:
                        items.append((x, R[x]["pdb_name"] in names_at(p)))
                        # next start with a different name set (adjacent same-name overloads
                        # are not a contradiction; DS2: counting them gave 305/30,283)
                        j = bisect.bisect_right(pstarts, p)
                        while j < len(pstarts) and names_at(pstarts[j]) == names_at(p):
                            j += 1
                        if j < len(pstarts):
                            cn += 1; ca += R[x]["pdb_name"] in names_at(pstarts[j])
                        break
            finally:
                unhide(x, s_)
        got = {}
        for x in keys:
            r = R[x]
            if not r["name_status"].startswith("AMBIGUOUS") or x in new:
                continue
            ps = r2p.get(x, set())
            if len(ps) != 1:
                continue
            pa = next(iter(ps))
            al = names_at(pa)
            if len(al) < 2 or r["obj"] not in {q["obj"] for q in procs[pa]}:
                continue
            got[x] = (pa, "evmap_start;aliases=%d" % len(al))
        cntp = collections.Counter(v[0] for v in got.values())
        g = G.judge("N9_AMBIGUOUS_ALIASES", items, control=(cn, ca),
                    require={"n_ge_20": len(items) >= 20, "unique_proto_start": all(c == 1 for c in cntp.values())},
                    extra={"population_ambiguous_rows": sum(1 for x in keys if R[x]["name_status"].startswith("AMBIGUOUS")),
                           "claim": "alias list = every PDB name at the evidence-map proto start",
                           "control_kind": "next proc start with a different name set holds the true name"})
        report["methods"]["N9"] = g
        if g["gate"]:
            for x, (pa, ev) in sorted(got.items()):
                new[x] = (pa, "N9", ev + ";proto=0x%08X" % pa)
            g["admitted"] = len(got)
        ctx.log("N9 gate=%s admitted=%d" % (g["gate"], g.get("admitted", 0)))

    # ---------------- residue stages (stages/naming_ext.py) ----------------
    # Every stage there is gated by the same engine; they fill `new` (proc targets) and `extname`
    # (targets without a single PDB proc: thunks, publics, manual rows) and may revise claims.
    extname = {}
    if _dk and not _hit:
        DEVC.store(ctx.work, "naming", _dk, {"new": new, "claimed": claimed, "name_of": name_of, "pa_of": pa_of,
                                             "report": report, "greps": {k: v for k, v in G.report.items() if k not in _greps0}})
    from . import naming_ext as NX
    S = NX.State(dict(locals(), size=size_registered))  # X0 re-derives from the registered extents
    hook = __import__("os").environ.get("PDBCHAIN_DEV_HOOK")
    if hook:  # development only: hand the live state to a driver (never set in a real run)
        __import__("importlib").import_module(hook).hook(S)
    NX.run(S)
    post = __import__("os").environ.get("PDBCHAIN_DEV_POSTHOOK")
    if post:  # development only: inspect the state after the residue stages
        __import__("importlib").import_module(post).hook(S)
    claimed = S.claimed
    ctx.alloc_updates = getattr(S, "alloc_updates", {})  # A8: obj from the name's PDB module

    # ---------------- residue census ----------------
    def why(x, sz=size):
        r = R[x]
        if x in getattr(S, "interior_of", {}):
            return "interior_of:0x%08X (interior label of the previous procedure split off by the recompiler)" % S.interior_of[x]
        if x in S.tail_of:
            return "tail_of:0x%08X (lone blr split off by the recompiler; part of the previous procedure)" % S.tail_of[x]
        if x in S.allow:
            return "extent_allowlisted (boundary is the recompiler's choice)"
        if not r["obj"]:
            return "no_obj(UNDETERMINABLE)"
        dl, dr, _ = bracket(x)
        if dl is None:
            return "no_bracket"
        reasons = []
        for d in ([dl] if dl == dr else [dl, dr]):
            p = x - d
            if p not in procs:
                c = contrib_at(p)
                reasons.append("target_not_proc_start" if c and c[2] == r["obj"] else "target_outside_obj")
            elif p in claimed:
                reasons.append("target_claimed")
            elif pname(p) is None:
                reasons.append("folded_names_at_target")
            elif pobj(p) != r["obj"]:
                reasons.append("target_other_obj")
            elif not size_ok(sz[x], psize(p)):
                reasons.append("size_mismatch")
            elif shape(x, p) < SHAPE_MIN:
                reasons.append("shape_mismatch")
            elif callee_check(x, p)[1]:
                reasons.append("callee_contradiction")
            else:
                reasons.append("sibling_ambiguity")
        # primary = the side that passed the most checks (CENSUS_ORDER: furthest first), not the
        # alphabetical minimum (fixed 2026-10-07); every side's reason is recorded
        prim = min(reasons, key=CENSUS_ORDER.index)
        return prim if len(reasons) == 1 else "%s (sides: %s)" % (prim, ",".join(reasons))

    residue = {x: why(x) for x in keys if R[x]["name_status"] == "NONE" and x not in new}
    # census before vs after extent normalisation (X0), on the same residue set
    if getattr(S, "ext_size", None):
        prim = lambda v: v.split(" (", 1)[0].split(":", 1)[0]
        report["census_extent_normalisation"] = {
            "raw_extents": dict(sorted(collections.Counter(prim(why(x)) for x in residue if x not in S.tail_of and x not in S.allow).items())),
            "normalised_extents": dict(sorted(collections.Counter(prim(why(x, S.ext_size)) for x in residue if x not in S.tail_of and x not in S.allow).items())),
            "tail_of_rows": sum(1 for x in residue if x in S.tail_of), "allowlisted_rows": sum(1 for x in residue if x in S.allow)}
        residue = {x: why(x, S.ext_size) for x in residue}

    cnt = collections.Counter(v[0] for v in new.values() if v[0] is not None)
    if any(c > 1 for c in cnt.values()):
        raise SystemExit("REFUSED: a procedure was assigned twice")

    def names_for(x):
        """The claim of row x: (sorted names, primary). Proc targets: every PDB name at the proc;
        residue-stage targets (thunks, publics, manual rows): the stage's own list."""
        if x in extname:
            al = list(extname[x]["names"])
            return al, al[0]
        al = sorted({q["name"] for q in procs[new[x][0]]})
        return al, al[0]

    def check(table):
        seen_p, seen_x = set(), set()
        for x, pa, nm in table:
            if x in extname:
                ok_name = nm in extname[x]["names"] and (pa is None or pa == new[x][0])
                status_ok = R[x]["name_status"] == "NONE"
            else:
                folded = len({q["name"] for q in procs.get(pa, ())}) > 1
                ok_name = (nm in {q["name"] for q in procs.get(pa, ())} and R[x]["obj"] in {q["obj"] for q in procs[pa]}) if folded \
                    else (pname(pa) == nm and pobj(pa) == R[x]["obj"])
                n9 = new.get(x, (None, ""))[1] == "N9"
                status_ok = R[x]["name_status"].startswith("AMBIGUOUS") if n9 else R[x]["name_status"] == "NONE"
            if not ok_name or not status_ok or x in seen_x or (pa is not None and pa in seen_p):
                return False
            if pa is not None:
                seen_p.add(pa)
            seen_x.add(x)
        return True

    table = [(x, new[x][0], names_for(x)[1]) for x in sorted(new)]
    shake = {"clean_passes": check(table)}
    plain = [i for i, (x, pa, _) in enumerate(table) if x not in extname and R[x]["obj"] in obj_procs]
    if plain:
        t2 = list(table); i0 = plain[len(plain) // 2]; x0, p0, _ = t2[i0]
        op = obj_procs[R[x0]["obj"]]
        # the neighbouring proc that another row already claims: moving a row onto it is a wrong
        # table the check MUST catch (moving onto an unclaimed same-obj proc is self-consistent)
        tgt = {pa for _, pa, _ in table if pa is not None}
        j0 = op.index(p0) if p0 in op else 0
        q = next((op[(j0 + d) % len(op)] for d in range(1, len(op)) if op[(j0 + d) % len(op)] in tgt), op[(j0 + 1) % len(op)])
        t2[i0] = (x0, q, sorted({r_["name"] for r_ in procs[q]})[0] if q in procs else "")
        shake["shifted_row_detected"] = not check(t2)
        shake["duplicate_row_detected"] = not check(table + [table[0]])
    ext_rows = [i for i, (x, _, _) in enumerate(table) if x in extname]
    if ext_rows:
        t3 = list(table); i0 = ext_rows[len(ext_rows) // 2]; x0, p0, _ = t3[i0]
        t3[i0] = (x0, p0, extname[x0]["names"][0] + "#perturbed")
        shake["residue_stage_name_perturbed_detected"] = not check(t3)
    report["shake"] = shake
    before = sum(1 for r in rows if r["name_status"] in NAMED or r["name_status"] == "PDB_PARENT+OFFSET")
    report["named_before"] = before
    report["named_after"] = before + len(new)
    report["admitted_by_method"] = dict(sorted(collections.Counter(v[1] for v in new.values()).items()))
    if not all(shake.values()):
        raise SystemExit("REFUSED: names table check failed or its shake was not detected: %s" % shake)

    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_NONE, escapechar="\\")
    w.writerow(HEADER)
    for x in sorted(new):
        pa, m, ev = new[x]
        al, nm = names_for(x)
        oc = extname[x]["obj_check"] if x in extname else ("obj_in" if len(al) > 1 else "obj_eq")
        w.writerow(["0x%08X" % x, m, nm, ctx.ident_for(nm, x), "" if pa is None else "0x%08X" % pa, oc, ev,
                    " ## ".join(al) if len(al) > 1 else ""])
    return buf.getvalue().encode("utf-8"), new, residue, report

