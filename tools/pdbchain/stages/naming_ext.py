"""Residue naming stages: run by stages/naming.py after N1-N9, on the functions those left
unnamed. Same gate engine and rules (LOO on hidden known answers of the same shape, a control
that must fail, salted tune/held-out split, Wilson bound, unique resolution); a stage admits
only when its gate passed. Order = strength:

  X0  extent normalisation (pre-stage, pluggable `naming.extent_normalisers`): retail extents as
      the recompiler registered them -> PDB-procedure-shaped extents. `tail_blr`: a registered
      function ending in an unconditional `b` followed by a lone `blr` that the recompiler
      registered as its own function (no .pdata entry) is one procedure; the lone `blr` is
      reported as a codegen finding (TAIL_OF), never named, never changed in codegen.
  T6  public_code rows: the evidence map's public_code row at the retail start
  T1  thunks by content: adjustor (addi r3,r3,-K ; b T) keyed by K + T's name, vcall by slot
      offset, import by the ordinal words, against prototype S_THUNK32 / S_PUB32 addresses that
      have no procedure; semantic check: the mangled public's adjustor{K}/vcall{off} must equal
      the retail operand and the thunk's class must equal the branch target's class
  T2  vtable slots: evidence-map vtable rows; slot i of the retail table -> slot i of the proto
      table when >= 2 other known slots of the table agree and none disagrees (fixpoint)
  T4  trailing-blr extents: proto proc ends `b ; blr`, retail extent (normalised by X0, or
      proto size - 4) holds the same masked words, the next retail word is the `blr`
  T3  single mapped data reference / string text tie-break: the retail formed addresses, mapped
      to proto through the evidence map's data rows (offset kept), are formed by exactly one
      unclaimed proc of the obj / the string texts are a subset of exactly one window proc's
  T5  call-site order: named callers whose bl list has the proto caller's length place x at the
      proto callee in the same position (fixpoint); targets already claimed are reported as
      contradictions with the first-come claims, not admitted
  T7  optimal assignment (Hungarian) per gap between named obj-mates: unnamed functions vs
      unclaimed procs, score = opcode-token similarity under size/callee checks; admits pairs
      whose assignment beats every alternative by a margin
  A8  name -> obj (reverse path) for UNDETERMINABLE rows named by an obj-free stage: the name's
      PDB module gives the obj; gated on allocated rows named by the same stages
  M   MANUAL_EVIDENCE: hand-reviewed names (input `manual_names`), only where no gated stage
      named the function; a row contradicting a gated stage is refused and reported
"""
from __future__ import annotations

import bisect, collections, csv, difflib, gzip, random, re, subprocess

from ..arch import ppc
from ..common import size_ok
from ..util import tsv

BLR = 0x4E800020
NAMED = ("PDB_NAME", "PDB_NAME_BYTES")


class State:
    """The naming pass's live state (dicts/sets are shared, so stages mutate it in place)."""

    def __init__(self, d):
        self.__dict__.update(d)


# ---------------------------------------------------------------- helpers
def words(rd, va, n):
    b = rd(va, n) if n > 0 else None
    return [ppc.word(b, k) for k in range(0, len(b) - 3, 4)] if b else []


def is_b(w):
    return w >> 26 == 18 and (w & 3) == 0


def btarget(w, va):
    return (va + ppc.branch_disp(w)) & 0xFFFFFFFF


def msvc_num(s, i):
    """MSVC encoded number at s[i] -> (value, next index); None on a parse failure."""
    if i >= len(s):
        return None
    neg = s[i] == "?"
    i += neg
    if i < len(s) and s[i].isdigit():
        return (-(int(s[i]) + 1) if neg else int(s[i]) + 1), i + 1
    v = 0
    while i < len(s) and "A" <= s[i] <= "P":
        v = v * 16 + ord(s[i]) - 65
        i += 1
    if i >= len(s) or s[i] != "@":
        return None
    return (-v if neg else v), i + 1


ADJ_RE = re.compile(r"@@([GHOPWX])(?=[0-9A-P?])")


def adjustor_of(mangled):
    """this-adjustor K of a mangled thunk public (`...@@W<K>...`), or None."""
    out = None
    for m in ADJ_RE.finditer(mangled):
        r = msvc_num(mangled, m.end())
        if r and r[1] < len(mangled) and mangled[r[1]] in "AEIQ":
            out = r[0]
    return out


VCALL_RE = re.compile(r"^\?\?_9(.+?)@@\$B")


def vcall_of(mangled):
    m = VCALL_RE.match(mangled)
    if not m:
        return None
    r = msvc_num(mangled, m.end())
    return r[0] if r else None


def demangle_names(undname, names):
    """undname 0x1000 (name only): `Ns::Class::member`, the shape PDB proc names have."""
    out = {}
    names = sorted({n for n in names if n.startswith("?")})
    if not undname or not names:
        return out
    batches, cur, ln = [], [], 0
    for n in names:  # Windows command lines are limited to 32,767 chars
        if cur and (ln + len(n) + 3 > 24000 or len(cur) >= 200):
            batches.append(cur); cur, ln = [], 0
        cur.append(n); ln += len(n) + 3
    if cur:
        batches.append(cur)
    for b in batches:
        r = subprocess.run([undname, "0x1000"] + b, capture_output=True, text=True, errors="replace")
        for m in re.finditer(r'Undecoration of :- "(.*)"\r?\nis :- "(.*)"', r.stdout):
            out[m.group(1)] = m.group(2)
    return out


def klass(name):
    n = name.split(":", 1)[1] if name.startswith("[thunk]:") else name
    # strip a trailing special-name member: X::`vector deleting destructor'
    i = n.rfind("::")
    return n[:i] if i > 0 else None


def hungarian(cost):
    """Minimum-cost assignment for an n x m matrix (n <= m). Returns col index per row."""
    n, m = len(cost), len(cost[0]) if cost else 0
    INF = float("inf")
    u, v, p, way = [0.0] * (n + 1), [0.0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [INF] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], INF, 0
            for j in range(1, m + 1):
                if not used[j]:
                    c = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if c < minv[j]:
                        minv[j], way[j] = c, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta; v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]; p[j0] = p[j1]; j0 = j1
            if not j0:
                break
    out = [None] * n
    for j in range(1, m + 1):
        if p[j]:
            out[p[j] - 1] = j - 1
    return out


# ---------------------------------------------------------------- the stages
def run(S):
    C, G, ctx, R, keys = S.C, S.G, S.ctx, S.R, S.keys
    rrd, prd, procs, names_at, pname, pobj, psize = S.rrd, S.prd, S.procs, S.names_at, S.pname, S.pobj, S.psize
    new, claimed, name_of, pa_of, extname, report = S.new, S.claimed, S.name_of, S.pa_of, S.extname, S.report
    methods = set(C.get("residue_stages", ["X0", "T6", "T1", "T2", "T4", "T3", "T5", "T7", "T8", "T9", "X1", "A8", "M"]))
    SHAPE_MIN = S.SHAPE_MIN
    rep = report.setdefault("residue_stages", {})
    keyset = set(keys)
    idx = {x: i for i, x in enumerate(keys)}

    tail_of, allow = {}, set()

    def residue(x):
        return (R[x]["name_status"] == "NONE" and x not in new and x not in tail_of and x not in allow
                and x not in S.__dict__.get("interior_of", {}))

    # ---- the names a retail function carries (any method) and its known proto start ----
    def rnames(x):
        if x in extname:
            return set(extname[x]["names"])
        if x in pa_of and pa_of[x] in procs:
            return set(names_at(pa_of[x]))
        n = name_of.get(x)
        if n:
            return {n}
        r = R.get(x)
        return {r["pdb_name"]} if r and r["pdb_name"] and r["name_status"] in NAMED else set()

    def known_pa(x):
        return pa_of.get(x)

    # ---- prototype publics and thunks ----
    pubs = collections.defaultdict(list)
    for r in S.P.get("publics", ()):
        pubs[int(r["proto_va"], 16)].append(r["name"])
    thk = collections.defaultdict(list)
    for r in S.P["thunks"]:
        thk[int(r["proto_va"], 16)].append(r)
    dm = demangle_names(ctx.inputs.get("undname"), [n for q, v in pubs.items() if q not in procs for n in v])

    def pub_names(q):
        """Display names of a public-only address (thunk / public without a proc)."""
        out = set()
        for n in pubs.get(q, ()):
            d = dm.get(n, n)
            k = adjustor_of(n)
            if k is not None and "adjustor{" not in d:
                d += "`adjustor{%d}'" % k
            out.add(d)
        return sorted(out)

    def target_names(q):
        return set(names_at(q)) if q in procs else set(pub_names(q))

    def name_ext(x, q, method, ev, names, obj_check):
        new[x] = (q, method, ev + (";proto=0x%08X" % q if q is not None else ""))
        extname[x] = {"names": sorted(names), "obj_check": obj_check}
        if q is not None:
            claimed.add(q)
            pa_of[x] = q
        name_of[x] = sorted(names)[0]

    def admit_proc_or_pub(got, method):
        """got: x -> (q, ev). Unique-resolution: a proto target taken by two functions is refused."""
        cnt = collections.Counter(q for q, _ in got.values())
        k, refused = 0, collections.Counter()
        for x, (q, ev) in sorted(got.items()):
            if cnt[q] > 1:
                refused["target_proposed_twice"] += 1
                continue
            if q in claimed or not residue(x):
                refused["target_claimed_or_named"] += 1
                continue
            ns = target_names(q)
            if not ns:
                refused["no_name_at_target"] += 1
                continue
            obj = R[x]["obj"]
            if q in procs:
                objs = {p["obj"] for p in procs[q]}
                if obj and obj not in objs:
                    refused["target_obj_differs"] += 1
                    continue
                oc = ("obj_in" if len(ns) > 1 else "obj_eq") if obj else "no_obj"
            else:
                tobjs = {t["obj"] for t in thk.get(q, ())}
                if obj and tobjs and obj not in tobjs:
                    refused["target_obj_differs"] += 1
                    continue
                oc = ("obj_eq" if tobjs else "obj_unchecked") if obj else "no_obj"
            name_ext(x, q, method, ev, ns, oc)
            k += 1
        return k, dict(refused)

    # =========================== X0 extent normalisation ===========================
    # Normalisers are pluggable (`naming.extent_normalisers`, applied in order) and write a derived
    # extent table only; codegen is never changed. ReXGlue has no link order (registration is
    # address-sorted, TUs are byte-budget fills; docs/research/REXGLUE_LINK_ORDER.md), so no
    # normaliser reads TU numbers.
    #   pdata     : a registered start that is a .pdata begin takes the raw .pdata length
    #               (ReXGlue's own size is grown by SEH scope tables for 4 functions)
    #   tail_blr  : a function ending in an unconditional `b` followed by a registered 4-byte
    #               `blr` with no .pdata entry absorbs it (GapFill splits the unreachable blr,
    #               phase_gapfill.cpp:56-58); the blr row becomes TAIL_OF:<prev>, never named
    # `inputs.extent_allowlist` (the flaw sweep's allowlisted boundaries: ReXGlue's choice) is
    # excluded from every residue stage's LOO denominators and claims.
    ext_size = dict(S.size)
    normalisers = C.get("extent_normalisers", ["pdata", "tail_blr"])
    pdlen = {}
    im = getattr(ctx, "retail_image", None)
    if im is not None:
        try:
            pdlen = {p["va"]: p["size"] for p in im.pdata()}
        except Exception:
            pdlen = {}
    if ctx.inputs.get("extent_allowlist"):
        for line in open(ctx.inputs["extent_allowlist"], encoding="utf-8"):
            f = line.rstrip("\n").split("\t")
            if len(f) > 1 and f[1].startswith("0x"):
                allow.add(int(f[1], 16))
    S.allow = allow
    x0 = {"normalisers": normalisers, "pdata_entries": len(pdlen), "extent_allowlist": len(allow)}
    if "X0" in methods:
        from ..extents import normalise
        ext_size, t_of, st = normalise(keys, S.size, rrd, pdlen, allow, normalisers)
        tail_of.update(t_of)
        x0.update(st)
        if "tail_blr" in normalisers:
            x0["lone_blr_already_named"] = sum(1 for y in tail_of if R[y]["name_status"] != "NONE" or y in new)
            x0["lone_blr_in_residue"] = sum(1 for y in tail_of if R[y]["name_status"] == "NONE" and y not in new)
            x0["prev_function_named"] = sum(1 for y in tail_of if tail_of[y] in name_of)
        x0["finding"] = ("registered 4-byte `blr` directly after an unconditional `b`: the prototype procedure's "
                         "unreachable trailing blr split off by ReXGlue GapFill; codegen unchanged, reported as TAIL_OF")
        # exact-size agreement on known answers, before vs after normalisation
        eq_b = eq_a = ok_b = ok_a = n_ = 0
        for x, pa in S.truth.items():
            ps = psize(pa)
            n_ += 1
            eq_b += S.size[x] == ps; eq_a += ext_size[x] == ps
            ok_b += size_ok(S.size[x], ps); ok_a += size_ok(ext_size[x], ps)
        x0["known_answers"] = {"n": n_, "exact_size_before": eq_b, "exact_size_after": eq_a,
                               "size_ok_before": ok_b, "size_ok_after": ok_a}
    rep["X0_EXTENTS"] = x0
    S.ext_size, S.tail_of = ext_size, tail_of
    # known answers for every residue stage's LOO: the allowlisted ReXGlue boundaries are excluded
    TR = {x: p for x, p in S.truth.items() if x not in allow and x not in tail_of}

    # =========================== T6 public_code rows ===========================
    pubcode = collections.defaultdict(set)
    vt_rows, dmap = [], []
    for r in tsv(S.evmap_path):
        if not r["retail_addr"] or r["bytes_class"] not in ("EXACT", "DELTA"):
            continue
        ra, pa = int(r["retail_addr"], 16), int(r["proto_addr"], 16)
        if r["kind"] in ("public_code", "thunk"):
            pubcode[ra].add(pa)
        if r["kind"] == "vtable":
            vt_rows.append((ra, pa))
        if r["kind"] in ("data", "string", "public_data", "vtable", "rtti", "table"):
            dmap.append((ra, pa))
    dmap = sorted(set(dmap))
    dst = [d[0] for d in dmap]
    dext = {}
    for i, (ra, pa) in enumerate(dmap):
        nx = dmap[i + 1][0] if i + 1 < len(dmap) else ra + 4
        dext[ra] = min(nx - ra, 4096)

    def r2p_data(a):
        i = bisect.bisect_right(dst, a) - 1
        if i >= 0 and dst[i] <= a < dst[i] + dext[dst[i]]:
            return dmap[i][1] + (a - dst[i])
        return None

    if "T6" in methods:
        uniq = {x: next(iter(v)) for x, v in pubcode.items() if len(v) == 1 and x in keyset}
        items, cn, ca = [], 0, 0
        pk = sorted(set(uniq.values()))
        for x, q in uniq.items():
            t = TR.get(x)
            if t is None:
                continue
            items.append((x, q == t))
            j = bisect.bisect_right(pk, q)
            if j < len(pk):
                cn += 1; ca += pk[j] == t
        g = G.judge("T6_PUBLIC_CODE", items, control=(cn, ca),
                    extra={"rule": "evidence-map public_code/thunk row at the retail start, one proto address",
                           "control_kind": "the next mapped public's proto address equals the truth"})
        rep["T6"] = g
        if g["gate"]:
            got = {x: (q, "public_code_row") for x, q in uniq.items() if residue(x)}
            g["admitted"], g["refused"] = admit_proc_or_pub(got, "T6")
        ctx.log("T6 gate=%s admitted=%s" % (g["gate"], g.get("admitted")))

    # =========================== T1 thunks by content ===========================
    def tnames_retail(t):
        return rnames(t)

    def key_at(rd, va, n, tn):
        ws = words(rd, va, min(n, 16)) if n >= 8 else []
        if len(ws) < 2:
            return None
        a, b = ws[0], ws[1]
        if a >> 16 == 0x3863 and is_b(b):
            return ("adj", ppc.sx16(a & 0xFFFF), frozenset(tn(btarget(b, va + 4))))
        if len(ws) >= 4:
            c, d = ws[2], ws[3]
            if a == 0x81830000 and b >> 16 == 0x816C and c == 0x7D6903A6 and d == 0x4E800420:
                return ("vcall", b & 0xFFFF)
            if c == 0x7D6903A6 and d == 0x4E800420 and a >> 24 == 1 and b >> 24 == 2:
                return ("imp", a, b)
        return None

    tpool = sorted({q for q in set(thk) | set(pubs) if q not in procs})
    pidx_adj, pidx = collections.defaultdict(list), collections.defaultdict(list)
    for q in tpool:
        k = key_at(prd, q, 16, target_names)
        if not k:
            continue
        if k[0] == "adj":
            pidx_adj[k[1]].append((q, k[2]))
        else:
            pidx[k].append(q)

    def semantic(k, q):
        """Mangled public at q must carry the same adjustor / vcall offset; adjustor thunk class
        (S_THUNK32 name) must equal the retail branch target's class."""
        if k[0] == "adj":
            ks = {adjustor_of(n) for n in pubs.get(q, ())} - {None}
            if ks != {-k[1]}:
                return False, "adjustor_offset"
            tcls = {klass(t["name"]) for t in thk.get(q, ())} - {None}
            rcls = {klass(n) for n in k[2]} - {None}
            if not tcls or not (tcls & rcls):
                return False, "class"
            return True, ""
        if k[0] == "vcall":
            vs = {vcall_of(n) for n in pubs.get(q, ())} - {None}
            return (vs == {k[1]}), "vcall_offset"
        return bool(pubs.get(q)), "no_public"

    def thunk_match(x, perturb=0):
        k = key_at(rrd, x, S.size[x], tnames_retail)
        if not k:
            return None, None
        if perturb:
            k = {"adj": lambda: ("adj", k[1] - 4, k[2]), "vcall": lambda: ("vcall", k[1] + 4),
                 "imp": lambda: ("imp", k[1] + 1, k[2] + 1)}[k[0]]()
        if k[0] == "adj":
            hits = [q for q, ns in pidx_adj.get(k[1], ()) if k[2] & ns]
        else:
            hits = list(pidx.get(k, ()))
        hits = [q for q in hits if semantic(k, q)[0]]
        return (hits[0] if len(hits) == 1 else None), k[0]

    # =========================== T2 vtable slots ===========================
    tables = []
    for ra, pa in sorted(set(vt_rows)):
        n = dext.get(ra, 0)
        rws, pws = words(rrd, ra, n), words(prd, pa, n)
        sl = []
        for rw, pw in zip(rws, pws):
            if rw not in keyset:
                break
            sl.append((rw, pw))
        if len(sl) >= 3:
            tables.append((ra, pa, sl))

    def vt_votes(shift=0, hide=None):
        votes = collections.defaultdict(set)
        dec = []
        for ra, pa, sl in tables:
            pws = [pw for _, pw in sl]
            ks = []
            for j, (rw, _) in enumerate(sl):
                pw = pws[j + shift] if 0 <= j + shift < len(pws) else None
                kp = None if rw == hide else known_pa(rw)
                ks.append(None if kp is None or pw is None else kp == pw)
            for i, (rw, _) in enumerate(sl):
                pw = pws[i + shift] if 0 <= i + shift < len(pws) else None
                if pw is None:
                    continue
                others = [v for j, v in enumerate(ks) if j != i and v is not None and sl[j][0] != rw]
                if len(others) >= 2 and all(others):
                    votes[rw].add(pw)
                    if ks[i] is not None:
                        dec.append(((ra, i), ks[i]))
        return votes, dec

    if "T2" in methods:
        votes, items = vt_votes()
        _, cdec = vt_votes(shift=1)
        ca = sum(1 for _, ok in cdec if ok)
        allslots = sum(len(sl) for _, _, sl in tables)
        g = G.judge("T2_VTABLE_SLOT", items, control=(len(items), ca),
                    extra={"tables": len(tables), "slots": allslots,
                           "rule": ">= 2 other known slots of the table agree, none disagrees",
                           "control_kind": "proto slots shifted by one: clean-table agreement"})
        rep["T2"] = g
        if g["gate"]:
            tot, rounds = 0, []
            ref = collections.Counter()
            for rnd in range(6):
                votes, _ = vt_votes()
                got = {x: (next(iter(s)), "vtable_slot;round=%d" % rnd) for x, s in votes.items() if residue(x) and len(s) == 1}
                ref["conflicting_slots"] += sum(1 for x, s in votes.items() if residue(x) and len(s) > 1)
                k, rf = admit_proc_or_pub(got, "T2")
                for a, b in rf.items():
                    ref[a] += b
                rounds.append(k); tot += k
                if not k:
                    break
            g["admitted"], g["rounds"], g["refused"] = tot, rounds, dict(ref)
        ctx.log("T2 gate=%s admitted=%s" % (g["gate"], g.get("admitted")))

    # =========================== T1 thunks by content (after T2: T2 supplies adjustor known answers) =====
    if "T1" in methods:
        tps = set(tpool)
        known = {x: next(iter(v)) for x, v in pubcode.items() if len(v) == 1 and x in keyset and next(iter(v)) in tps}
        # adjustor thunks have no mapped evidence-map row; T2 (vtable slots, independent evidence) named them
        known.update({x: v[0] for x, v in new.items() if v[1] == "T2" and v[0] in tps and x not in known})
        items, cn, ca, sub = [], 0, 0, collections.defaultdict(list)
        for x, q in sorted(known.items()):
            got, kind = thunk_match(x)
            if kind is None:
                continue
            if got is not None:
                items.append((x, got == q)); sub[kind].append(got == q)
            cg, _ = thunk_match(x, perturb=1)
            cn += 1; ca += cg == q
        g = G.judge("T1_THUNK_CONTENT", items, control=(cn, ca),
                    require={"n_ge_20": len(items) >= 20},
                    extra={"known_answers": "retail thunks with an evidence-map public_code/thunk row, plus thunks named by T2 vtable slots (the matcher reads neither)",
                           "by_kind": {k: {"decided": len(v), "correct": sum(v)} for k, v in sorted(sub.items())},
                           "proto_pool": len(tpool), "control_kind": "key perturbed (K-4 / slot+4 / ordinal+1): the true thunk is still recovered"})
        rep["T1"] = g
        if g["gate"]:
            got, kinds = {}, collections.Counter()
            for x in keys:
                if residue(x):
                    q, kind = thunk_match(x)
                    if q is not None:
                        got[x] = (q, "thunk_content;kind=%s" % kind); kinds[kind] += 1
            g["admitted"], g["refused"] = admit_proc_or_pub(got, "T1")
            g["matched_by_kind"] = dict(kinds)
        ctx.log("T1 gate=%s admitted=%s" % (g["gate"], g.get("admitted")))

    # =========================== T4 trailing-blr extents ===========================
    def tail_rule(x, q):
        ps = psize(q) if q in procs else 0
        if ps < 8:
            return False
        pw = words(prd, q, ps)
        if len(pw) < 2 or pw[-1] != BLR or not is_b(pw[-2]):
            return False
        rs = ext_size.get(x, S.size[x])
        if not (ps - 4 <= rs <= ps + 16 + ps // 16):
            return False
        rw = words(rrd, x, ps)
        if len(rw) != len(pw) or rw[-1] != BLR:
            return False
        return [ppc.evmap_mask(w) for w in rw[:-1]] == [ppc.evmap_mask(w) for w in pw[:-1]]

    def t4(x, obj, taken, shift=0):
        op = S.obj_procs.get(obj, [])
        hits = set()
        for t in S.n1_targets(x):
            i = bisect.bisect_left(op, t)
            for j in range(max(0, i - S.NEIGH), min(len(op), i + S.NEIGH + 1)):
                jj = j + shift
                if not 0 <= jj < len(op):
                    continue
                q = op[jj]
                if q in taken or pname(q) is None or S.callee_check(x, q)[1]:
                    continue
                if tail_rule(x, q):
                    hits.add(q)
        return next(iter(hits)) if len(hits) == 1 else None

    if "T4" in methods:
        items, cn, ca = [], 0, 0
        # Known answers: the evidence-map truth (TR) plus every function already named by a gated
        # stage that ran before T4 (N1-N9, T6, T1, T2) whose proto target is a proc. The second
        # source is a gated (>= 0.99) label, so any label noise can only lower the measurement.
        KA = dict(TR)
        src = collections.Counter(truth=len(TR))
        for x, v in new.items():
            q = v[0]
            if x in KA or x in allow or x in tail_of or q is None or q not in procs:
                continue
            KA[x] = q
            src[v[1]] += 1
        for x, pa in sorted(KA.items()):
            ps = psize(pa)
            pw = words(prd, pa + ps - 8, 8) if ps >= 8 else []
            if len(pw) != 2 or pw[1] != BLR or not is_b(pw[0]):
                continue
            taken = claimed - {pa}
            got = t4(x, R[x]["obj"], taken)
            if got is not None:
                items.append((x, got == pa))
            cg = t4(x, R[x]["obj"], taken, shift=2 * S.NEIGH + 1)
            cn += 1; ca += cg is not None and cg != pa
        g = G.judge("T4_TRAILING_BLR", items, control=(cn, ca),
                    extra={"population": "named functions whose proto proc ends `b ; blr`",
                           "known_answer_sources": dict(src),
                           "decided_by_source": dict(collections.Counter(
                               "truth" if x in TR else new[x][1] for x, _ in items)),
                           "control_kind": "candidate window shifted past the bracket target"})
        rep["T4"] = g
        if g["gate"]:
            got = {}
            for x in keys:
                if residue(x) and R[x]["obj"] and x not in tail_of:
                    q = t4(x, R[x]["obj"], claimed)
                    if q is not None:
                        got[x] = (q, "trailing_blr;retail_extent=%d;proto_size=%d" % (S.size[x], psize(q)))
            g["admitted"], g["refused"] = admit_proc_or_pub(got, "T4")
        ctx.log("T4 gate=%s admitted=%s" % (g["gate"], g.get("admitted")))

    # =========================== T3 data-ref / string-text tie-break ===========================
    pform = {}

    def pformed(q):
        if q not in pform:
            pform[q] = set(ppc.formed(prd(q, psize(q))))
        return pform[q]

    rform = {}

    def rmapped(x):
        if x not in rform:
            rform[x] = {a for a in (r2p_data(v) for v in ppc.formed(rrd(x, S.size[x]))) if a is not None}
        return rform[x]

    def ptexts(q):
        return {t for t in (S.text_at(prd, a) for a in pformed(q)) if t}

    def rtexts(x):
        return {t for t in (S.text_at(rrd, a) for a in ppc.formed(rrd(x, S.size[x]))) if t}

    def cand_ok(x, q, obj, taken):
        return (q not in taken and pname(q) is not None and pobj(q) == obj
                and size_ok(ext_size.get(x, S.size[x]), psize(q)) and S.shape(x, q) >= SHAPE_MIN
                and S.callee_check(x, q)[1] == 0)

    def t3_ref(x, obj, taken, refs=None):
        ra = rmapped(x) if refs is None else refs
        if not ra:
            return None
        hits = [q for q in S.obj_procs.get(obj, []) if ra <= pformed(q) and cand_ok(x, q, obj, taken)]
        return hits[0] if len(hits) == 1 else None

    def t3_str(x, obj, taken, texts=None):
        rs = rtexts(x) if texts is None else texts
        if not rs:
            return None
        op = S.obj_procs.get(obj, [])
        hits = set()
        for t in S.n1_targets(x):
            i = bisect.bisect_left(op, t)
            for q in op[max(0, i - 3):i + 4]:
                if rs <= ptexts(q) and cand_ok(x, q, obj, taken):
                    hits.add(q)
        return next(iter(hits)) if len(hits) == 1 else None

    if "T3" in methods:
        rng = random.Random(33)
        tk = sorted(TR)
        smp = rng.sample(tk, min(8000, len(tk)))
        for kind, fn, ev_of in (("T3_DATAREF", t3_ref, rmapped), ("T3_STRING", t3_str, rtexts)):
            items, cn, ca = [], 0, 0
            for x in smp:
                pa = TR[x]
                taken = claimed - {pa}
                got = fn(x, R[x]["obj"], taken)
                if got is not None:
                    items.append((x, got == pa))
                y = rng.choice(tk)
                if y != x and ev_of(y):
                    cg = fn(x, R[x]["obj"], taken, ev_of(y))
                    cn += 1; ca += cg is not None and cg != pa
            g = G.judge(kind, items, control=(cn, ca), extra={"sample": len(smp),
                        "control_kind": "another function's mapped refs / strings: wrong proc accepted"})
            rep[kind[:2] + ("R" if kind.endswith("REF") else "S")] = g
            if g["gate"]:
                got = {}
                for x in keys:
                    if residue(x) and R[x]["obj"]:
                        q = fn(x, R[x]["obj"], claimed)
                        if q is not None:
                            got[x] = (q, kind.lower())
                g["admitted"], g["refused"] = admit_proc_or_pub(got, kind[:2] + ("R" if kind.endswith("REF") else "S"))
            ctx.log("%s gate=%s admitted=%s" % (kind, g["gate"], g.get("admitted")))

    # =========================== T5 call-site order ===========================
    callers = collections.defaultdict(set)
    for c, ts in S.rcalls.items():
        for t in ts:
            callers[t].add(c)

    def t5(x, shift=0):
        v, bad = collections.Counter(), 0
        for c in callers.get(x, ()):
            P_ = known_pa(c)
            if P_ is None or P_ not in procs:
                continue
            rc, pc = S.rcalls[c], S.pcalls(P_)
            if len(rc) != len(pc) or not rc:
                continue
            tg = {pc[i + shift] for i, t in enumerate(rc) if t == x and 0 <= i + shift < len(pc)}
            if len(tg) == 1:
                v[next(iter(tg))] += 1
            else:
                bad += 1
        if len(v) != 1 or bad:
            return None
        p = next(iter(v))
        return p if p in procs and pname(p) is not None else None

    if "T5" in methods:
        items, cn, ca = [], 0, 0
        for x, pa in TR.items():
            got = t5(x)
            if got is not None:
                items.append((x, got == pa))
            cg = t5(x, shift=1)
            cn += 1; ca += cg is not None and cg == pa
        g = G.judge("T5_CALLSITE_ORDER", items, control=(cn, ca),
                    extra={"control_kind": "call position shifted by one in the proto caller"})
        rep["T5"] = g
        if g["gate"]:
            tot, rounds, contra = 0, [], []
            ref = collections.Counter()
            for rnd in range(6):
                got = {}
                for x in keys:
                    if residue(x):
                        q = t5(x)
                        if q is None:
                            continue
                        if q in claimed:
                            if rnd == 0:
                                owner = [y for y, v in new.items() if v[0] == q] or [y for y, p in pa_of.items() if p == q]
                                contra.append({"addr": "0x%08X" % x, "proto": "0x%08X" % q,
                                               "claimed_by": ["0x%08X %s" % (y, new[y][1] if y in new else "allocation")
                                                              for y in owner[:2]]})
                            continue
                        got[x] = (q, "callsite_order;round=%d" % rnd)
                k, rf = admit_proc_or_pub(got, "T5")
                for a, b in rf.items():
                    ref[a] += b
                rounds.append(k); tot += k
                if not k:
                    break
            g["admitted"], g["rounds"], g["refused"] = tot, rounds, dict(ref)
            # the same rule on every already-named function vs the claim it holds (first-come)
            agree = collections.Counter()
            disagree = []
            for x, p in sorted(pa_of.items()):
                if p not in procs:
                    continue
                q = t5(x)
                if q is None:
                    continue
                m = new[x][1] if x in new else "allocation"
                agree[(m, q == p)] += 1
                if q != p:
                    disagree.append({"addr": "0x%08X" % x, "method": m, "claim": "0x%08X" % p, "callsite": "0x%08X" % q})
            g["vs_existing_claims"] = {"agree": sum(v for (m, ok), v in agree.items() if ok),
                                       "disagree": sum(v for (m, ok), v in agree.items() if not ok),
                                       "by_method": {"%s|%s" % (m, "agree" if ok else "disagree"): v for (m, ok), v in sorted(agree.items())},
                                       "disagreements": disagree[:50]}
            g["contradicts_first_come_claims"] = {"count": len(contra), "by_claimer_method": dict(collections.Counter(
                c["claimed_by"][0].split()[1] if c["claimed_by"] else "?" for c in contra)), "examples": contra[:20]}
        ctx.log("T5 gate=%s admitted=%s" % (g["gate"], g.get("admitted")))

    # =========================== T7 optimal assignment ===========================
    by_obj = collections.defaultdict(list)
    for x in keys:
        if R[x]["obj"]:
            by_obj[R[x]["obj"]].append(x)

    def score(x, q):
        if not size_ok(ext_size.get(x, S.size[x]), psize(q)) or S.callee_check(x, q)[1]:
            return None
        return S.sim(x, q)

    def assign_gap(o, xl, xr, taken, hide=(), shift=0):
        pl, pr = known_pa(xl), known_pa(xr)
        if pl is None or pr is None or pr <= pl or pl not in procs or pr not in procs:
            return {}
        xs = by_obj[o]
        run_ = [y for y in xs if xl < y < xr and (residue(y) or y in hide)]
        op = S.obj_procs.get(o, [])
        j0, j1 = bisect.bisect_right(op, pl), bisect.bisect_left(op, pr)
        if shift:
            j0, j1 = j0 + shift * (j1 - j0 + 1), j1 + shift * (j1 - j0 + 1)
        cand = [q for q in op[j0:j1] if q not in taken and pname(q)]
        if not run_ or not cand or len(run_) > 12 or len(cand) > 16:
            return {}
        flip = len(run_) > len(cand)
        A, B = (cand, run_) if flip else (run_, cand)
        sc = [[(score(b, a) if flip else score(a, b)) for b in B] for a in A]
        cost = [[(1.0 - s) if s is not None else 1e6 for s in row] for row in sc]
        asg = hungarian(cost)
        out = {}
        for i, j in enumerate(asg):
            if j is None or sc[i][j] is None:
                continue
            x, q = (B[j], A[i]) if flip else (A[i], B[j])
            s = sc[i][j]
            alt = max([v for jj, v in enumerate(sc[i]) if jj != j and v is not None] or [0.0])
            alt2 = max([(score(x, qq) or 0.0) for qq in cand if qq != q] or [0.0])
            if s >= T7_T and s - max(alt, alt2) >= T7_M:
                out[x] = (q, s)
        return out

    T7_T, T7_M = float(C.get("t7_min_sim", 0.9)), float(C.get("t7_margin", 0.1))
    if "T7" in methods:
        rng = random.Random(77)
        tk = sorted(TR)
        smp = rng.sample(tk, min(5000, len(tk)))
        items, cn, ca = [], 0, 0
        for x in smp:
            o = R[x]["obj"]
            xs = by_obj[o]
            i = bisect.bisect_left(xs, x)
            H = [y for y in xs[i:i + rng.choice((1, 2, 3, 5))] if y in TR]
            if not H or H[0] != x:
                continue
            l = next((y for y in reversed(xs[:i]) if known_pa(y) is not None and y not in H), None)
            r_ = next((y for y in xs[i + 1:] if known_pa(y) is not None and y not in H), None)
            if l is None or r_ is None:
                continue
            taken = claimed - {TR[y] for y in H}
            sv = {y: pa_of.pop(y) for y in H}
            try:
                got = assign_gap(o, l, r_, taken, hide=set(H))
                # control: the gap's proc window moved past itself (next window of the obj)
                cg = assign_gap(o, l, r_, taken, hide=set(H), shift=1)
            finally:
                pa_of.update(sv)
            for y, (q, _) in got.items():
                if y in H:
                    items.append((y, q == TR[y]))
            cn += len(H); ca += sum(1 for y, (q, _) in cg.items() if y in H and q != TR[y])
        g = G.judge("T7_OPTIMAL_ASSIGNMENT", items, control=(cn, ca),
                    extra={"min_sim": T7_T, "margin": T7_M, "sample": len(smp),
                           "control_kind": "gap's proto window moved to the next gap of the obj"})
        rep["T7"] = g
        if g["gate"]:
            got = {}
            for o, xs in by_obj.items():
                named = [x for x in xs if known_pa(x) is not None]
                for xl, xr in zip(named, named[1:]):
                    for x, (q, s) in assign_gap(o, xl, xr, claimed).items():
                        if residue(x):
                            got[x] = (q, "assignment;sim=%.3f" % s)
            g["admitted"], g["refused"] = admit_proc_or_pub(got, "T7")
        ctx.log("T7 gate=%s admitted=%s" % (g["gate"], g.get("admitted")))

    # =========================== T8 strict-constant sibling tie-break ===========================
    # Sibling ambiguity (N1's target and >= 1 other proc within +-k pass every N1 check): the one
    # candidate whose body is strict-equal (ppc.strict_equal_words: only branch displacements and
    # lis/addi address immediates may differ; struct offsets and constants must be identical), whose
    # formed data addresses agree with the evidence map wherever it maps them, and whose extent is
    # exact. Unique among the window's candidates, or refused.
    def t8_ok(x, q):
        ps = psize(q)
        if ext_size.get(x, S.size[x]) != ps or ps < 8:
            return False
        rb, pb = rrd(x, ps), prd(q, ps)
        if not rb or not pb or not ppc.strict_equal_words(pb, rb):
            return False
        for pa_, ra_ in ppc.formed_pairs(pb, rb):
            m = r2p_data(ra_)
            if m is not None and m != pa_:
                return False
        return True

    def t8(x, obj, taken, shift=0, need_tie=True):
        op = S.obj_procs.get(obj, [])
        cands = set()
        for t in S.n1_targets(x):
            i = bisect.bisect_left(op, t)
            for j in range(max(0, i - S.NEIGH), min(len(op), i + S.NEIGH + 1)):
                jj = j + shift
                if 0 <= jj < len(op) and cand_ok(x, op[jj], obj, taken):
                    cands.add(op[jj])
        if need_tie and len(cands) < 2:
            return None, len(cands)
        hits = [q for q in cands if t8_ok(x, q)]
        return (hits[0] if len(hits) == 1 else None), len(cands)

    if "T8" in methods:
        items, cn, ca, ties = [], 0, 0, 0
        for x, pa in sorted(TR.items()):
            obj = R[x]["obj"]
            if not obj:
                continue
            # hide the whole +-k window's claims (the residue's siblings are unnamed too), so the
            # known answers face the same sibling ties as the population
            op = S.obj_procs.get(obj, [])
            win = set()
            for t in S.n1_targets(x):
                i = bisect.bisect_left(op, t)
                win.update(op[max(0, i - S.NEIGH):i + S.NEIGH + 1])
            taken = claimed - win - {pa}
            got, nc = t8(x, obj, taken)
            if nc < 2:
                continue
            ties += 1
            if got is not None:
                items.append((x, got == pa))
            cg, _ = t8(x, obj, taken, shift=2 * S.NEIGH + 1, need_tie=False)
            cn += 1; ca += cg is not None and cg != pa
        g = G.judge("T8_STRICT_TIEBREAK", items, control=(cn, ca),
                    extra={"population": "known answers with >= 2 window candidates passing N1's checks (sibling ties)",
                           "ties": ties, "control_kind": "candidate window shifted past the bracket target"})
        rep["T8"] = g
        if g["gate"]:
            got = {}
            for x in keys:
                if residue(x) and R[x]["obj"] and x not in tail_of:
                    q, nc = t8(x, R[x]["obj"], claimed)
                    if q is not None:
                        got[x] = (q, "strict_tiebreak;candidates=%d" % nc)
            g["admitted"], g["refused"] = admit_proc_or_pub(got, "T8")
        ctx.log("T8 gate=%s admitted=%s" % (g["gate"], g.get("admitted")))

    # =========================== T9 RTTI-keyed vtable slots ===========================
    # A retail vtable (a run of registered code pointers in a data section, preceded by a
    # complete-object-locator pointer) is keyed by its COL: (type-descriptor name, COL offset).
    # The proto vtable with the same key is found through the `??_7` publics (their COL at -4).
    # Slot k -> proto slot k. Unlike T2 this needs no evidence-map vtable row, so it reaches
    # vtables E1 never mapped (and, through A8, their functions' objs). A function stored in
    # several vtables must get one prediction from all of them.
    def be32(rd, a):
        b = rd(a, 4)
        return int.from_bytes(b, "big") if b and len(b) == 4 else None

    def col_key(rd, colp):
        if colp is None:
            return None
        sig, off, td = be32(rd, colp), be32(rd, colp + 4), be32(rd, colp + 12)
        if sig != 0 or off is None or td is None:
            return None
        nb = rd(td + 8, 256)
        if not nb or not nb.startswith(b".?A"):
            return None
        nm = nb.split(b"\0", 1)[0]
        return (nm.decode("latin-1"), off) if nb.find(b"\0") > 0 else None

    def t9_tables():
        # proto: key -> [proto vtable start]
        pv = collections.defaultdict(set)
        for q, ns in pubs.items():
            if any(n.startswith("??_7") for n in ns):
                k = col_key(prd, be32(prd, q - 4))
                if k:
                    pv[k].add(q)
        # retail: scan non-executable sections for runs of registered code pointers
        rv = {}
        if im is not None:
            for sec in im.sections:
                if sec["executable"]:
                    continue
                try:
                    blob = im.read_va(sec["va"], sec["available_size"] & ~3)
                except Exception:
                    continue
                n = len(blob) // 4
                ws = [int.from_bytes(blob[4 * i:4 * i + 4], "big") for i in range(n)]
                i = 1
                while i < n:
                    if ws[i] in keyset and ws[i - 1] not in keyset:
                        j = i
                        while j < n and ws[j] in keyset:
                            j += 1
                        k = col_key(rrd, ws[i - 1])
                        if k:
                            rv[sec["va"] + 4 * i] = (k, ws[i:j])
                        i = j
                    else:
                        i += 1
        return pv, rv

    def t9_pred(pv, rv, shift=0):
        """x -> set of proto targets predicted by every retail vtable slot holding x."""
        out = collections.defaultdict(set)
        for v, (k, fs) in rv.items():
            ps = pv.get(k)
            if not ps or len(ps) != 1:
                continue
            q0 = next(iter(ps))
            for s_, x in enumerate(fs):
                w = be32(prd, q0 + 4 * (s_ + shift))
                out[x].add(w if (w is not None and target_names(w)) else None)
        return out

    if "T9" in methods:
        pv, rv = t9_tables()
        pred, predc = t9_pred(pv, rv), t9_pred(pv, rv, shift=1)
        items, cn, ca = [], 0, 0
        for x, qs in sorted(pred.items()):
            pa = known_pa(x)
            if pa is None or x in tail_of or x in allow:
                continue
            q = next(iter(qs)) if len(qs) == 1 else None
            if q is not None:
                items.append((x, q == pa))
            cq = predc.get(x, set())
            cn += 1; ca += len(cq) == 1 and next(iter(cq)) == pa
        g = G.judge("T9_RTTI_VTABLE_SLOTS", items, control=(cn, ca),
                    extra={"retail_vtables_with_col": len(rv), "proto_vtable_keys": len(pv),
                           "proto_keys_unique": sum(1 for v in pv.values() if len(v) == 1),
                           "population": "named functions stored in an RTTI-keyed retail vtable",
                           "control_kind": "proto slot shifted by one"})
        rep["T9"] = g
        if g["gate"]:
            got = {}
            for x, qs in pred.items():
                if residue(x) and x not in tail_of and len(qs) == 1:
                    q = next(iter(qs))
                    if q is not None:
                        got[x] = (q, "rtti_vtable_slot;vtables=%d" % sum(1 for v in rv.values() if x in v[1]))
            g["admitted"], g["refused"] = admit_proc_or_pub(got, "T9")
        ctx.log("T9 gate=%s admitted=%s" % (g["gate"], g.get("admitted")))

    # =========================== X1 interior pieces (INTERIOR_OF) ===========================
    # A registered function x whose previous registered function y is placed on proc P with delta
    # d, where x - d lies strictly inside P and x's bytes equal P's bytes there (masked compare,
    # over x's whole extent): the recompiler split P at an interior label. x is part of P, not a
    # function of its own: reported INTERIOR_OF:<y>, never named, out of the naming denominator.
    # Must-fail controls: the rule run on every known separate function (truth) and on bytes
    # read at x+4 must fire < 1%.
    interior_of = {}

    def x1_rule(x, shift=0):
        i = idx[x]
        if i == 0:
            return None
        y = keys[i - 1]
        P = known_pa(y)
        if P is None or P not in procs:
            return None
        p = x - (y - P)
        ps = psize(P)
        if not P < p < P + ps:
            return None
        n = min(ext_size.get(x, S.size[x]), P + ps - p)
        if n < 4:
            return None
        rb, pb = rrd(x + shift, n), prd(p, n)
        return y if rb and pb and ppc.code_equal(pb, rb) else None

    if "X1" in methods:
        kn = [x for x in sorted(TR)]
        fp = sum(1 for x in kn if x1_rule(x) is not None)
        for x in keys:
            if residue(x) and x not in tail_of:
                y = x1_rule(x)
                if y is not None:
                    interior_of[x] = y
        sh = sum(1 for x in interior_of if x1_rule(x, shift=4) is not None)
        ok = bool(kn) and fp / len(kn) < G.control_bar and (not interior_of or sh / len(interior_of) < G.control_bar)
        rep["X1_INTERIOR"] = {"rows": len(interior_of), "fires_on_known_functions": {"n": len(kn), "fired": fp},
                              "fires_with_bytes_shifted_4": {"n": len(interior_of), "fired": sh}, "gate": ok}
        if not ok:
            interior_of.clear()
        ctx.log("X1 interior rows=%d gate=%s" % (len(interior_of), ok))
    S.interior_of = interior_of

    # =========================== A8 name -> obj (UNDETERMINABLE) ===========================
    S.alloc_updates = {}
    if "A8" in methods:
        OBJFREE = {"T1", "T2", "T5", "T6", "T9"}

        def module_of(x):
            q = new[x][0]
            if q in procs:
                return pobj(q)
            ts = {t["obj"] for t in thk.get(q, ())}
            return next(iter(ts)) if len(ts) == 1 else None

        items, cn, ca = [], 0, 0
        named_free = [x for x, v in new.items() if v[1] in OBJFREE and R[x]["obj"] and R[x]["class"] != "FOLDED"]
        for x in named_free:
            m = module_of(x)
            if m is None:
                continue
            items.append((x, m == R[x]["obj"]))
            j = idx[x] + 1
            while j < len(keys) and R[keys[j]]["obj"] == R[x]["obj"]:
                j += 1
            if j < len(keys) and R[keys[j]]["obj"]:
                cn += 1; ca += m == R[keys[j]]["obj"]
        g = G.judge("A8_NAME_TO_OBJ", items, control=(cn, ca),
                    extra={"population": "allocated rows named by an obj-free stage (T1/T2/T5/T6)",
                           "control_kind": "the next obj in retail order equals the name's module"})
        rep["A8"] = g
        if g["gate"]:
            for x, v in sorted(new.items()):
                if v[1] in OBJFREE and R[x]["class"] == "UNDETERMINABLE":
                    q = v[0]
                    m = module_of(x)
                    if m is None:
                        continue
                    src = ""
                    if q in procs:
                        ps = [p for p in procs[q] if p["obj"] == m]
                        src = ps[0].get("src_file", "") if ps else ""
                    lib = next((p["lib"] for p in procs.get(q, ()) if p["obj"] == m), "") or \
                        next((t["lib"] for t in thk.get(q, ()) if t["obj"] == m), "")
                    S.alloc_updates[x] = {"obj": m, "lib": lib, "src_file": src, "basis": "name=%s;proto=0x%08X" % (v[1], q)}
            g["admitted"] = len(S.alloc_updates)
        ctx.log("A8 gate=%s admitted=%s" % (g["gate"], g.get("admitted")))

    # =========================== M manual evidence ===========================
    mpath = ctx.inputs.get("manual_names")
    if "M" in methods and mpath:
        applied, refused, contra = 0, collections.Counter(), []
        rows_m = list(csv.DictReader(open(mpath, encoding="utf-8", newline=""), delimiter="\t"))
        prop_targets = collections.Counter(r["proto_proc"] for r in rows_m if r["proto_proc"])
        for r in rows_m:
            x = int(r["addr"], 16)
            q = int(r["proto_proc"], 16) if r["proto_proc"] else None
            nm = r["proposed_name"]
            if x not in R:
                refused["unknown_addr"] += 1
                continue
            if x in new or R[x]["name_status"] != "NONE":
                have = rnames(x) or {R[x]["pdb_name"]}
                if nm in have:
                    refused["already_named_same"] += 1
                elif q is not None and pa_of.get(x) == q:
                    # same proto target; the hand row spells the name differently (undname
                    # signature form vs the name-only form the stages emit)
                    refused["already_named_same_target"] += 1
                else:
                    refused["contradicts_gated_stage"] += 1
                    contra.append({"addr": r["addr"], "manual": nm, "gated": sorted(have)[:3],
                                   "method": new[x][1] if x in new else R[x]["name_status"]})
                continue
            if q is not None and (q in claimed or prop_targets[r["proto_proc"]] > 1):
                refused["target_claimed_by_gated_stage" if q in claimed else "target_proposed_twice"] += 1
                if q in claimed:
                    owner = [y for y, v in new.items() if v[0] == q]
                    contra.append({"addr": r["addr"], "manual": nm, "target": r["proto_proc"],
                                   "claimed_by": ["0x%08X %s" % (y, new[y][1]) for y in owner[:2]] or ["allocation"]})
                continue
            if x in tail_of:
                refused["lone_blr_tail"] += 1
                continue
            new[x] = (q, "MANUAL_EVIDENCE", "manual_evidence:" + r["evidence"].replace("\t", " ")[:300])
            extname[x] = {"names": [nm], "obj_check": "manual"}
            if q is not None:
                claimed.add(q); pa_of[x] = q
            name_of[x] = nm
            applied += 1
        rep["M"] = {"input": mpath, "rows": len(rows_m), "applied": applied, "refused": dict(refused),
                    "contradictions": contra, "gate": True}
        ctx.log("M manual applied=%d refused=%s" % (applied, dict(refused)))
    S.claimed = claimed
    return rep
