"""pdbchain command line: run the declared chain from a TOML config.

    python tools/pdbchain --config my_title.toml [--only evidence|allocation|naming] [--dry]

Exit 0 and 'PDBCHAIN DONE' only when every admitted stage passed its gate, the output tables
pass their invariants and their self-shakes detect the perturbations. A gate failure of a core
check is a refusal (exit 2, nothing written). A second run with the same inputs writes 0 files.
"""
from __future__ import annotations

import argparse, collections, json, sys, time, tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .arch import ppc
from .gates import GateEngine
from .paths import TreePaths, ident_for
from .readers import pdb as PDB
from .readers import registered as REG
from .readers.pe import PEImage, reader
from .stages import allocation as ALLOC
from .stages import evidence as EVID
from .stages import naming as NAMING
from . import devcache as DEVC
from .util import Writer, sha256_bytes, sha256_file, table_text, tsv

VERSION = "pdbchain 1.0"


@dataclass
class Context:
    cfg: dict
    work: Path
    engine: GateEngine
    writer: Writer
    inputs: dict
    pdb_reader_dir: str
    primary: dict
    tree: TreePaths
    rrd: object = None
    prd: object = None
    quiet: bool = False
    t0: float = field(default_factory=time.time)

    def log(self, msg):
        if not self.quiet:
            print("[%7.1fs] %s" % (time.time() - self.t0, msg), flush=True)

    @staticmethod
    def ident_for(name, addr):
        return ident_for(name, addr)


def load_config(path):
    cfg = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    base = Path(path).resolve().parent

    def res(p):
        if not p:
            return p
        q = Path(p)
        return str(q if q.is_absolute() else (base / q).resolve())
    for k, v in list(cfg.get("inputs", {}).items()):
        if isinstance(v, str) and k != "undname" or k == "undname" and v and ("/" in v or "\\" in v):
            cfg["inputs"][k] = res(v)
    for p in cfg.get("prototype", []):
        for k in ("pe", "pdb", "tables"):
            if p.get(k):
                p[k] = res(p[k])
    if cfg.get("registered", {}).get("path"):
        cfg["registered"]["path"] = res(cfg["registered"]["path"])
    if cfg.get("registered", {}).get("calls"):
        cfg["registered"]["calls"] = res(cfg["registered"]["calls"])
    cfg.setdefault("output", {})["dir"] = res(cfg.get("output", {}).get("dir", "pdbchain_out"))
    return cfg


def pdb_tables(ctx, proto):
    """Extract (or reuse a cached extraction of) a prototype's procs/contribs/dcontribs/thunks.
    The cache is keyed by the PE and PDB sha256; a stale cache is re-extracted."""
    label = proto.get("label", proto["role"])
    d = Path(proto["tables"]) if proto.get("tables") else ctx.work / ("pdb_" + label)
    stamp = d / "source.json"
    want = {"pe_sha256": sha256_file(proto["pe"]), "pdb_sha256": sha256_file(proto["pdb"]), "tool": VERSION}
    names = {"procs": PDB.PROC_HEADER, "contribs": PDB.CONTRIB_HEADER, "dcontribs": PDB.CONTRIB_HEADER,
             "thunks": PDB.THUNK_HEADER, "publics": PDB.PUBLIC_HEADER}
    if not (stamp.is_file() and json.loads(stamp.read_text(encoding="utf-8")).get("pe_sha256") == want["pe_sha256"]
            and json.loads(stamp.read_text(encoding="utf-8")).get("pdb_sha256") == want["pdb_sha256"]
            and all((d / ("%s.tsv.gz" % n)).is_file() for n in names)):
        ctx.log("pdb tables: extracting %s (%s)" % (label, proto["pdb"]))
        t = PDB.extract_tables(ctx.pdb_reader_dir, proto["pe"], proto["pdb"])
        for n, h in names.items():
            ctx.writer.write(d / ("%s.tsv.gz" % n), table_text(h, t[n]), gz=True)
        ctx.writer.write_json(stamp, want | {"stats": t["stats"]})
    return {n: list(tsv(d / ("%s.tsv.gz" % n))) for n in names}, want


def gate_table(report):
    out = []
    for k, g in sorted(report.items()):
        c = g.get("control", {})
        out.append("%-26s %-5s decided %6d prec %.5f wilson %.5f held %5d/%-5d ctrl %s %s" % (
            k, "PASS" if g["gate"] else "fail", g["decided"], g["precision"], g["wilson95_lower"], g["held"]["correct"],
            g["held"]["decided"], "%d/%d" % (c.get("accepted", 0), c.get("population", 0)) if c else "-",
            "; ".join(g["refusal_reasons"])))
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pdbchain", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--only", choices=("evidence", "allocation", "naming"), help="stop after this stage")
    ap.add_argument("--dry", action="store_true", help="measure and gate, write no outputs")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--dev-cache", action="store_true", help="development only: reuse cached allocation / N1-N9 results whose code+inputs are unchanged")
    a = ap.parse_args(argv)
    t0 = time.time()
    cfg = load_config(a.config)
    work = Path(cfg["output"]["dir"])
    gc = cfg.get("gate", {})
    engine = GateEngine(bar=float(gc.get("precision", 0.99)), control_bar=float(gc.get("control", 0.01)),
                        min_n=int(gc.get("min_n", 1)), salt=gc.get("salt", "pdbchain-split-v1"),
                        overrides=gc.get("overrides", {}), require_heldout=bool(gc.get("require_heldout", True)),
                        wilson_bar=float(gc["wilson"]) if gc.get("wilson") is not None else 0.99)
    protos = cfg.get("prototype", [])
    prim = [p for p in protos if p.get("role") == "primary"]
    if len(prim) != 1:
        print("REFUSED: exactly one [[prototype]] with role = 'primary' is required", file=sys.stderr)
        return 2
    inputs = cfg.get("inputs", {})
    ctx = Context(cfg, work, engine, Writer(), inputs, cfg.get("pdb", {}).get("reader_dir", ""), prim[0],
                  TreePaths(inputs.get("source_roots")), quiet=a.quiet)
    if a.dry:
        ctx.writer = Writer()
        ctx.writer.write = lambda *x, **k: False  # measure only
        ctx.writer.write_json = lambda *x, **k: False
    receipt = {"tool": VERSION, "arch": ppc.NAME, "gate_engine": {"bar": engine.bar, "control_bar": engine.control_bar,
               "min_n": engine.min_n, "wilson_bar": engine.wilson_bar, "salt": engine.salt, "require_heldout": engine.require_heldout,
               "self_shake": engine.shake}, "inputs": {}}
    for k in ("retail_pe", "anchors", "evidence_map", "keyed_feed", "mangled_names", "manual_accepts", "undeterminable", "source_roots"):
        if inputs.get(k):
            receipt["inputs"][k] = {"path": inputs[k], "sha256": sha256_file(inputs[k])}

    # ---- pinned external helpers ([pins] path = sha256) ----
    # The PDB reader modules (identity_donor.py, identity_image.py in [pdb] reader_dir) must be
    # pinned; any other external helper listed is checked too. A mismatch or a missing pin refuses.
    rdir = cfg.get("pdb", {}).get("reader_dir", "")
    pins = {str(Path(k)): v.lower() for k, v in cfg.get("pins", {}).items()}
    required = [str(Path(rdir) / f) for f in ("identity_donor.py", "identity_image.py")] if rdir else []
    pin_rep, bad = {}, []
    for f in sorted(set(pins) | set(required)):
        have = sha256_file(f) if Path(f).is_file() else None
        want = pins.get(f)
        ok = have is not None and want is not None and have == want
        pin_rep[f] = {"sha256": have, "pinned": want, "ok": ok}
        if not ok:
            bad.append(f)
    probe = next((f for f in pin_rep if pin_rep[f]["sha256"]), None)
    shake_pin = None
    if probe:  # self-shake: a perturbed copy's hash must not match the pin
        shake_pin = sha256_bytes(Path(probe).read_bytes() + b"\0") != pins.get(probe)
    receipt["inputs"]["pinned_helpers"] = {"files": pin_rep, "perturbed_copy_detected": shake_pin}
    if bad or shake_pin is False:
        print("PDBCHAIN REFUSED: external helper pin mismatch or missing %s" % bad, file=sys.stderr)
        return 2

    # ---- E1 evidence map ----
    if inputs.get("evidence_map"):
        evmap = inputs["evidence_map"]
        receipt["evidence"] = {"source": "input (E1 not run)"}
    else:
        ctx.log("E1 evidence map")
        summ, evmap = EVID.run(cfg, ctx)
        receipt["evidence"] = summ
        if evmap is None:
            print("PDBCHAIN REFUSED: evidence map gate failed", file=sys.stderr)
            receipt["gates"] = engine.report
            print(json.dumps(receipt, indent=1, sort_keys=True))
            return 2
    if a.only == "evidence":
        receipt["gates"] = engine.report
        print(json.dumps(receipt["evidence"], indent=1, sort_keys=True))
        print("PDBCHAIN DONE (evidence only)")
        return 0

    # ---- PDB tables ----
    P, pin = pdb_tables(ctx, prim[0])
    receipt["inputs"]["primary"] = {"label": prim[0].get("label"), "pe": prim[0]["pe"], "pdb": prim[0]["pdb"]} | pin
    aux = {}
    for p in protos:
        if p.get("role") == "aux":
            AP, apin = pdb_tables(ctx, p)
            aux[p.get("label", "aux")] = (AP, reader(PEImage(p["pe"])))
            receipt["inputs"].setdefault("aux", []).append({"label": p.get("label"), "pe": p["pe"], "pdb": p["pdb"]} | apin)
    rim, pim = PEImage(inputs["retail_pe"]), PEImage(prim[0]["pe"])
    ctx.rrd, ctx.prd = reader(rim), reader(pim)
    ctx.retail_image = rim  # residue stages: .pdata extents

    # ---- registered functions ----
    rc = cfg.get("registered", {})
    if rc.get("adapter", "rexglue") == "rexglue":
        reg = REG.rexglue(rc["path"], rc.get("prefix"))
    else:
        reg = REG.generic_tsv(rc["path"], rc.get("calls"), ctx.rrd)
    receipt["inputs"]["registered"] = {"source": reg.source, "functions": len(reg.funcs),
                                       "sha256": sha256_bytes("".join("%08X%s" % f for f in reg.funcs).encode())}
    ctx.log("registered functions: %d" % len(reg.funcs))

    # ---- A allocation ----
    ctx.log("A allocation")
    ctx.dev_cache = bool(a.dev_cache)
    ak = DEVC.key("alloc", ["stages/allocation.py", "arch/ppc.py", "common.py", "gates.py", "extents.py"], cfg,
                  sha256_file(evmap), receipt["inputs"]["registered"]["sha256"],
                  json.dumps(receipt["inputs"], sort_keys=True, default=str)) if a.dev_cache else None
    hit = DEVC.load(work, "alloc", ak) if ak else None
    if hit:
        rows, owners_of, arep, greps = hit
        engine.report.update(greps)
        receipt["dev_cache"] = ["alloc"]
    else:
        before = set(engine.report)
        rows, owners_of, arep = ALLOC.run(cfg, ctx, reg, P, aux, evmap, inputs.get("keyed_feed"))
        if ak:
            DEVC.store(work, "alloc", ak, (rows, owners_of, arep, {k: v for k, v in engine.report.items() if k not in before}))
    no_cp = arep.pop("no_counterpart")
    und_listed = {int(r["addr"], 16) for r in tsv(inputs["undeterminable"])} if inputs.get("undeterminable") else set()
    pending = {int(r[0], 16) for r in rows if r[2] == "UNDETERMINABLE" and r[3] == ALLOC.PENDING_A8_BASIS}
    und = und_listed | pending
    addrs = [x for x, _ in reg.funcs]
    shake = {"clean_passes": ALLOC.check_table(rows, addrs, und)}
    k = next((i for i, r in enumerate(rows) if r[2] != "UNDETERMINABLE"), None)
    if k is not None:
        t2 = [list(r) for r in rows]; t2[k][4] = ""
        shake["blanked_evidence_detected"] = not ALLOC.check_table(t2, addrs, und)
        shake["dropped_row_detected"] = not ALLOC.check_table(rows[:k] + rows[k + 1:], addrs, und)
    arep["shake"] = shake
    arep["class_counts"] = dict(sorted(collections.Counter(r[2] for r in rows).items()))
    arep["name_status_counts"] = dict(sorted(collections.Counter(r[9] for r in rows).items()))
    receipt["allocation"] = arep
    ctx.log("allocation classes %s" % arep["class_counts"])
    if not all(arep["checks"].values()) or not all(shake.values()):
        receipt["gates"] = engine.report
        print(gate_table(engine.report))
        print(json.dumps({"checks": arep["checks"], "shake": shake, "stages": arep["stages"]}, indent=1, default=str))
        print("PDBCHAIN REFUSED: allocation checks failed", file=sys.stderr)
        return 2
    alloc_text = table_text(ALLOC.HEADER, rows)
    owners_text = ("addr\towner_obj\n" + "".join("0x%08X\t%s\n" % (x, o) for x in sorted(owners_of) for o in owners_of[x])).encode()

    names_text, new, nres, nrep = b"", {}, {}, None
    if a.only != "allocation":
        ctx.log("N naming")
        ctx.dev_cache_key = DEVC.key("naming", ["stages/naming.py", "arch/ppc.py", "common.py", "gates.py", "extents.py"], cfg,
                                     sha256_file(evmap), table_text(ALLOC.HEADER, rows)) if a.dev_cache else None
        names_text, new, nres, nrep = NAMING.run(cfg, ctx, rows, P, aux, evmap)
        if getattr(ctx, "dev_cache_hit", False):
            receipt.setdefault("dev_cache", []).append("naming_N")
        receipt["naming"] = nrep
        # A8 NAME_TO_OBJ (gated in the naming pass): an UNDETERMINABLE row named by an obj-free
        # stage takes its obj from the name's PDB module
        upd = getattr(ctx, "alloc_updates", {}) or {}
        if upd:
            rows = [list(r) for r in rows]
            for r in rows:
                u = upd.get(int(r[0], 16))
                if u and r[2] == "UNDETERMINABLE":
                    r[2], r[3], r[4], r[5], r[6], r[7] = "NAME_MODULE", u["basis"], u["obj"], u["lib"], u["src_file"], ""
                    r[8] = ctx.tree.tree_path(u["src_file"] or u["obj"])
            shake2 = {"clean_passes": ALLOC.check_table(rows, addrs, und)}
            k2 = next((i for i, r in enumerate(rows) if r[2] == "NAME_MODULE"), None)
            t2 = [list(r) for r in rows]; t2[k2][8] = ""
            shake2["blanked_tree_path_detected"] = not ALLOC.check_table(t2, addrs, und)
            arep["name_module"] = {"rows": sum(1 for r in rows if r[2] == "NAME_MODULE"), "shake": shake2}
            arep["class_counts"] = dict(sorted(collections.Counter(r[2] for r in rows).items()))
            if not all(shake2.values()):
                print("PDBCHAIN REFUSED: allocation check after NAME_MODULE failed %s" % shake2, file=sys.stderr)
                return 2
            alloc_text = table_text(ALLOC.HEADER, rows)
    # final list check: every pending row resolved by A8, no listed row resolved (stale list),
    # and the table invariant against the listed rows only; shaken with one pending row left
    if pending or a.only != "allocation":
        left = sorted("0x%08X" % int(r[0], 16) for r in rows if r[2] == "UNDETERMINABLE" and r[3] == ALLOC.PENDING_A8_BASIS)
        stale = sorted(r[0] for r in rows if r[2] == "NAME_MODULE" and int(r[0], 16) in und_listed)
        fin = {"pending_a8": len(pending), "pending_left_n": len(left), "pending_left": left[:20], "listed_rows_resolved_by_a8": stale[:20],
               "clean_passes": ALLOC.check_table(rows, addrs, und_listed)}
        if pending:
            k3 = next(i for i, r in enumerate(rows) if int(r[0], 16) in pending)
            t3 = [list(r) for r in rows]; t3[k3][2:4] = ["UNDETERMINABLE", ALLOC.PENDING_A8_BASIS]
            fin["unresolved_pending_detected"] = not ALLOC.check_table(t3, addrs, und_listed)
        arep["undeterminable_list"] = fin
        if left:  # the full list, for the residue review that must list (or resolve) them
            (work / "pending_left.txt").write_text("".join(a_ + "\n" for a_ in left), encoding="utf-8")
        if left or stale or not fin["clean_passes"] or not fin.get("unresolved_pending_detected", True):
            print("PDBCHAIN REFUSED: undeterminable list check failed %s" % fin, file=sys.stderr)
            return 2
    und = und_listed

    # ---- residue ----
    res_rows = []
    for r in rows:
        x = int(r[0], 16)
        if r[2] == "UNDETERMINABLE":
            res_rows.append([r[0], "file", r[2], "every allocation method refused (listed with evidence in the undeterminable input)"
                             + ("; no masked-byte counterpart in any prototype PDB" if x in no_cp else "")])
        if x in nres:
            res_rows.append([r[0], "name", r[2], nres[x]])
    receipt["residue"] = {"file": sum(1 for r in res_rows if r[1] == "file"), "name": sum(1 for r in res_rows if r[1] == "name"),
                          "name_reasons": dict(sorted(collections.Counter("%s|%s" % (r[2], r[3]) for r in res_rows if r[1] == "name").items()))}
    # TAIL_OF rows (X0: a lone `blr` that is the previous procedure's tail) are not functions:
    # reported outside the naming denominator
    n_tail = sum(1 for r in res_rows if r[1] == "name" and r[3].startswith(("tail_of:", "interior_of:")))
    receipt["residue"]["denominator_excluding_tail_of"] = {
        "functions": len(rows) - n_tail, "tail_of": n_tail,
        "unnamed": receipt["residue"]["name"] - n_tail, "named": len(rows) - receipt["residue"]["name"]}
    res_text = table_text(["addr", "kind", "class", "reason"], res_rows)

    receipt["gates"] = engine.report
    receipt["gate_deviations"] = engine.deviations
    admitted = {"allocation": arep["class_counts"]}
    if nrep:
        admitted["naming"] = nrep["admitted_by_method"]
    receipt["admitted"] = admitted
    O = cfg["output"]
    outs = {"allocation": (work / O.get("allocation", "allocation.tsv.gz"), alloc_text),
            "owners": (work / O.get("owners", "allocation_owners.tsv.gz"), owners_text),
            "residue": (work / O.get("residue", "residue.tsv"), res_text)}
    if nrep:
        outs["names"] = (work / O.get("names", "names.tsv.gz"), names_text)
    receipt["outputs"] = {k: {"path": str(p), "sha256_uncompressed": sha256_bytes(t), "rows": t.count(b"\n") - 1}
                          for k, (p, t) in outs.items()}
    if a.dry:
        print(json.dumps(receipt["admitted"], indent=1))
        print("PDBCHAIN DRY (nothing written) %.1fs" % (time.time() - t0))
        return 0
    for k, (p, t) in outs.items():
        ctx.writer.write(p, t, gz=str(p).endswith(".gz"))
    ctx.writer.write_json(work / O.get("receipt", "receipt.json"), receipt)
    print(gate_table(engine.report))
    print(json.dumps(receipt["admitted"], indent=1))
    print("files changed: %d (%s)" % (len(ctx.writer.changed), ", ".join(Path(p).name for p in ctx.writer.changed) or "none"))
    print("runtime %.1fs" % (time.time() - t0))
    print("PDBCHAIN DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
