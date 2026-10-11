"""V0_PREV_BUILD: carry a previous build's proven allocation and names to the current build.

Config ([prev_build]):
  generated   codegen tree of the previous build (its DEFINE_REX_FUNC blocks)
  allocation  the previous build's allocation.tsv.gz      names  its names.tsv.gz (merged first)
  images      folder with the two loaded-image dumps: base.<VA>.<x|d>.bin and tu2.<VA>.<x|d>.bin
              (rexglue codegen with REX_DUMP_IMAGE=<folder>/base resp. /tu2)
  evidence    the previous build's evidence_map.tsv.gz; when inputs.evidence_map is not given the
              current build's E1 map is composed from it through the proven code + data maps (R4)
Current build: [registered] path.

Gates (R5/R6). Every proof class is judged by the GateEngine as stage V0_<class>:
  decisions  one per pair with >= 1 check site: rare-callee call sites (callee with <= 3 callers in
             the previous build) and data sites (an address formed at instruction k, translated by the
             proven data map; identical-code pairs only); correct when every site agrees
  control    every pair re-assigned to a random other previous-build function (seed 1313): accepted
             when all its rare-callee sites agree (chance agreement must stay under the control bar)
A class that fails the proven gate is carried as tier=probable when it meets PROBABLE, otherwise
dropped as a whole; both are listed in the receipt. P10 (identical stubs by address
order) and neighbour-file placement are not implemented: refused by measurement (docs/TU2_METHOD_
RECOMMENDATIONS.md, items 5).
Carry (R7): a proven row is the previous row byte for byte (addresses rewritten), UNDETERMINABLE included.
"""
from __future__ import annotations

import collections, csv, gzip, io
from pathlib import Path

from ..gates import wilson_lower

csv.field_size_limit(1 << 30)
CORE = Path(__file__).with_name("prevbuild_core.py")
CLASSES = ("P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9")
# Probable tier (user decision 2026-10-11): a class that fails the proven gate is still carried when
# precision >= 0.99, Wilson 95% lower bound >= 0.95 and control < 1%. Its rows are marked
# tier=probable in basis; tools/modapi/gen_hooks.py refuses hooks on them unless a mod opts in.
PROBABLE = {"precision": 0.99, "wilson": 0.95, "control": 0.01}


def _rows(p):
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE))


def _merged_prev(alloc, names):
    """previous allocation with its names table applied (the organizer's apply_names semantics)"""
    by = {int(r["addr"], 16): r for r in _rows(alloc)}
    if names and Path(names).exists():
        for n in _rows(names):
            r = by.get(int(n["addr"], 16))
            if r is None:
                continue
            r["name_status"] = "NAME_" + n["method"]
            r["pdb_name"] = n["pdb_name"]
            r["basis"] += ";name=%s" % n["method"]
            r["ident"] = n["ident"] if r["symbol"] == "sub_%08X" % int(n["addr"], 16) else ""
    return by


def compute(base_gen, cur_gen, img_dir, log=lambda *_: None):
    ns = {"BASE_GEN": str(base_gen), "CUR_GEN": str(cur_gen), "IMG_DIR": str(img_dir), "_log": log,
          "__name__": "prevbuild_core"}
    exec(compile(CORE.read_text(encoding="utf-8"), str(CORE), "exec"), ns)
    return ns


def judge(engine, ns):
    m, how, A, B, callers_a = ns["m"], ns["how"], ns["A"], ns["B"], ns["callers_a"]
    dtr, dmap = ns["dtr"], ns["dmap"]
    rare = {x for x, c in callers_a.items() if len(c) <= 3}

    def decision(b, a):
        cb, ca = B[b]["calls"], A[a]["calls"]
        if len(cb) != len(ca):
            return None
        sites = [m[xb] == xa for xb, xa in zip(cb, ca) if xb in m and xa in rare]
        # data sites: instruction-aligned formed addresses through the proven data map (only when
        # both sides form the same number of addresses, i.e. aligned code)
        da, db = A[a]["data"], B[b]["data"]
        if len(da) == len(db) and A[a]["norm"] == B[b]["norm"]:
            for xa, xb in zip(da, db):
                t = dtr(xa)
                if t is not None and xa in dmap:
                    sites.append(t == xb)
        return None if not sites else all(sites)

    by_class = collections.defaultdict(list)
    for b, a in m.items():
        d = decision(b, a)
        if d is not None:
            by_class[how[b]].append(("%08X" % b, d))
    # control: every pair re-assigned to a random other previous-build function (seeded); a
    # shift-by-one control is not used because neighbouring functions legitimately share callees
    import random
    mb = sorted(m); pool = [m[b] for b in mb]; random.Random(1313).shuffle(pool)
    shifted = [decision(b, a) for b, a in zip(mb, pool) if a != m[b]]
    shifted = [d for d in shifted if d is not None]
    control = (len(shifted), sum(1 for d in shifted if d))
    rep, failed, tiers = {}, [], {}
    for k in CLASSES:
        n_pairs = sum(1 for b in m if how[b] == k)
        if not n_pairs:
            continue
        g = engine.judge("V0_" + k, by_class.get(k, []), control,
                         extra={"pairs": n_pairs, "decided_note": "pairs with >= 1 rare-callee site"})
        items = by_class.get(k, [])
        nd, nk = len(items), sum(1 for _, ok in items if ok)
        prec = nk / nd if nd else 0.0
        wl = wilson_lower(nk, nd)
        crate = control[1] / control[0] if control[0] else 1.0
        if g["gate"]:
            tier = "proven"          # engine gate: precision/held-out >= bar, Wilson >= 0.99, control < bar
        elif nd and prec >= PROBABLE["precision"] and wl >= PROBABLE["wilson"] and crate < PROBABLE["control"]:
            tier = "probable"        # precision >= 0.99, Wilson >= 0.95, control < 1%: named, not hookable
        else:
            tier = None
        rep[k] = {"pairs": n_pairs, "tier": tier, "proven_gate": g["gate"], "decided": nd, "correct": nk,
                  "precision": round(prec, 5), "wilson95_lower": round(wl, 5), "control_rate": round(crate, 5),
                  "proven_refusal_reasons": g["refusal_reasons"]}
        tiers[k] = tier
        if tier is None:
            failed.append(k)
    keep = {b: a for b, a in m.items() if how[b] not in failed}
    return keep, rep, failed, {"population": control[0], "accepted": control[1], "tiers": tiers}


def carry(keep, how, prev_by, cur_registered, tiers=None):
    """R7: previous row verbatim for every proven pair; addresses rewritten."""
    out = {}
    for b, a in keep.items():
        r = prev_by.get(a)
        if r is None or (cur_registered is not None and b not in cur_registered):
            continue
        r = dict(r)
        oh, nh = "%08X" % a, "%08X" % b
        r["addr"] = "0x" + nh
        if r["symbol"] == "sub_" + oh:
            r["symbol"] = "sub_" + nh
        if r.get("ident"):
            r["ident"] = r["ident"].replace(oh, nh)
        r["basis"] += ";v0=%s:0x%s;tier=%s" % (how[b], oh, (tiers or {}).get(how[b], "proven"))
        out[b] = r
    return out


def compose_evidence(prev_evidence, ns, keep, out_path):
    """R4: the current E1 map from the previous one through the proven code map (identical-code pairs,
    same offset) and the instruction-aligned data map; unprovable rows are refused, never guessed."""
    import bisect
    A, B, dtr = ns["A"], ns["B"], ns["dtr"]
    same = {a: b for b, a in keep.items() if B[b]["norm"] == A[a]["norm"]}
    starts = sorted(same)
    lo = min(A); hi = max(A) + 0x10000

    def code_tr(x):
        i = bisect.bisect_right(starts, x) - 1
        if i < 0 or x - starts[i] >= 0x10000:
            return None
        return same[starts[i]] + (x - starts[i])
    rows = _rows(prev_evidence)
    kept = refused = 0
    for r in rows:
        if not r["retail_addr"]:
            continue
        x = int(r["retail_addr"], 16)
        if r["kind"] in ("proc", "public_code"):
            t = same.get(x)
        else:
            t = code_tr(x) if lo <= x < hi else dtr(x)
        if t is None:
            r["retail_addr"], r["bytes_class"], r["name_class"] = "", "REFUSED", "REFUSED_V0"
            refused += 1
        else:
            r["retail_addr"] = "0x%08X" % t
            kept += 1
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), delimiter="\t", quoting=csv.QUOTE_NONE, lineterminator="\n")
    w.writeheader(); w.writerows(rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wt", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())
    return {"rows_kept": kept, "rows_refused": refused, "data_addresses": len(ns["dmap"])}


def run(cfg, ctx, cur_registered):
    pb = cfg["prev_build"]
    base = Path(ctx.cfg_dir) if hasattr(ctx, "cfg_dir") else Path(".")
    ctx.log("V0 prev-build proofs")
    ns = compute(pb["generated"], cfg["registered"]["path_resolved"], pb["images"], lambda *a: ctx.log(" ".join(map(str, a))))
    keep, rep, failed, ctrl = judge(ctx.engine, ns)
    prev_by = _merged_prev(pb["allocation"], pb.get("names"))
    carried = carry(keep, ns["how"], prev_by, cur_registered, ctrl.get("tiers"))
    receipt = {"classes": rep, "refused_classes": failed, "control_random": ctrl,
               "tiers": {"proven": sorted(k for k, t in ctrl.get("tiers", {}).items() if t == "proven"),
                         "probable": sorted(k for k, t in ctrl.get("tiers", {}).items() if t == "probable"),
                         "probable_rule": PROBABLE},
               "pairs_kept": len(keep), "rows_carried": len(carried),
               "pair_gate_refused": len(ns.get("refused", [])), "conflicts": len(ns["conflicts"]),
               "not_implemented_refused_by_measurement": ["P10 identical stubs by order (90.6%)",
                                                          "neighbour-file placement (95.3% / 97.4%)"]}
    evmap = None
    if pb.get("evidence") and not ctx.inputs.get("evidence_map"):
        evmap = ctx.work / "evidence_map_v0.tsv.gz"
        receipt["evidence"] = compose_evidence(pb["evidence"], ns, keep, evmap)
    return carried, evmap, receipt
