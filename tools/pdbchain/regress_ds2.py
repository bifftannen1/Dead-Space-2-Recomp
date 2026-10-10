r"""DS2 regression test for pdbchain.

    python tools/pdbchain/regress_ds2.py [--config tools/pdbchain/examples/ds2.toml] [--skip-run]

1. runs the chain on the DS2 inputs (E1 evidence map from the PDB, PDB tables, allocation, naming)
2. evidence map: uncompressed sha256 must equal the R1-13 v1 receipt's evidence_map_sha256
3. allocation: must equal the LATEST COMMITTED config/allocation.tsv.gz and allocation_owners.tsv.gz
   (git show HEAD:...), byte for byte after decompression
4. names: compared with the latest committed config/names.tsv.gz; every differing row is listed
   with its method; PASS needs identical, or every difference attributed: a committed row from a
   method the chain does not run, or a chain-only row from a residue stage (--residue-methods,
   counted per method). A changed or missing committed row is never accepted. Allocation rows
   UNDETERMINABLE -> NAME_MODULE (A8) are the only accepted allocation difference
5. idempotence: a second run must report 'files changed: 0'
Prints REGRESS_DS2 PASS / FAIL; exit 0 only on PASS.
"""
from __future__ import annotations

import argparse, collections, csv, gzip, hashlib, io, json, os, subprocess, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
V1_RECEIPT = Path(os.environ.get("PDBCHAIN_V1_RECEIPT", "r1-13-pdb-evidence-map/receipt.json"))
csv.field_size_limit(1 << 30)


def committed(path):
    sha = subprocess.run(["git", "-C", str(REPO), "log", "-1", "--format=%H", "--", path], capture_output=True, text=True).stdout.strip()
    raw = subprocess.run(["git", "-C", str(REPO), "show", "HEAD:" + path], capture_output=True).stdout
    return gzip.decompress(raw), sha


def rows(text):
    return list(csv.DictReader(io.StringIO(text.decode("utf-8")), delimiter="\t", quoting=csv.QUOTE_NONE))


def run(cfg):
    t = time.time()
    p = subprocess.run([sys.executable, str(REPO / "tools" / "pdbchain"), "--config", cfg], capture_output=True, text=True)
    return p, time.time() - t


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(REPO / "tools" / "pdbchain" / "examples" / "ds2.toml"))
    ap.add_argument("--skip-run", action="store_true", help="compare the existing outputs only")
    ap.add_argument("--chain-methods", default="N1,N2,N3,N4,N5,N6,N7,N9")
    ap.add_argument("--residue-methods", default="T6,T1,T2,T4,T3R,T3S,T5,T7,T8,T9,MANUAL_EVIDENCE",
                    help="methods of stages/naming_ext.py: their rows are additions, listed per method")
    a = ap.parse_args(argv)
    import tomllib
    cfgd = tomllib.loads(Path(a.config).read_text(encoding="utf-8"))
    out = (Path(a.config).parent / cfgd["output"]["dir"]).resolve()
    res = {}
    if not a.skip_run:
        p1, t1 = run(a.config)
        (out / "regress_run1.log").write_text(p1.stdout + p1.stderr, encoding="utf-8")
        res["run1"] = {"exit": p1.returncode, "seconds": round(t1, 1), "done": "PDBCHAIN DONE" in p1.stdout}
        if p1.returncode:
            print(p1.stdout[-3000:], p1.stderr[-3000:])
            print("REGRESS_DS2 FAIL (chain exit %d)" % p1.returncode)
            return 1
    rec = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
    if rec.get("dev_cache"):
        print("REGRESS_DS2 FAIL (receipt from a --dev-cache run: %s; run the full chain)" % rec["dev_cache"])
        return 1
    v1 = json.loads(V1_RECEIPT.read_text(encoding="utf-8"))
    ev = rec["evidence"].get("evidence_map_sha256_uncompressed")
    res["evidence_map"] = {"chain": ev, "r1_13_v1_receipt": v1.get("evidence_map_sha256"), "identical": ev == v1.get("evidence_map_sha256")}

    ok = res["evidence_map"]["identical"]
    for name, path in (("allocation", "config/allocation.tsv.gz"), ("owners", "config/allocation_owners.tsv.gz")):
        ref, sha = committed(path)
        mine = gzip.decompress((out / Path(path).name).read_bytes())
        d = {"commit": sha, "identical": mine == ref, "rows_chain": mine.count(b"\n") - 1, "rows_committed": ref.count(b"\n") - 1}
        if mine != ref:
            rm = {r["addr"]: r for r in rows(mine)} if name == "allocation" else {}
            rr = {r["addr"]: r for r in rows(ref)} if name == "allocation" else {}
            diffs = [k for k in sorted(set(rm) | set(rr)) if rm.get(k) != rr.get(k)]
            d["differing_rows"] = len(diffs)
            if name == "allocation":
                expl = [k for k in diffs if rm.get(k, {}).get("class") == "NAME_MODULE"
                        and rr.get(k, {}).get("class") == "UNDETERMINABLE"]
                d["explained_name_module"] = len(expl)
                diffs = [k for k in diffs if k not in set(expl)]
            d["unexplained"] = len(diffs)
            d["examples"] = [{"addr": k, "chain": rm.get(k), "committed": rr.get(k)} for k in diffs[:10]]
        res[name] = d
        ok &= d["identical"] or d.get("unexplained", 1) == 0

    ref, sha = committed("config/names.tsv.gz")
    mine = gzip.decompress((out / "names.tsv.gz").read_bytes())
    nm = {r["addr"]: r for r in rows(mine)}
    nr = {r["addr"]: r for r in rows(ref)}
    only_c = sorted(set(nr) - set(nm))
    only_m = sorted(set(nm) - set(nr))
    changed = sorted(k for k in set(nm) & set(nr) if nm[k] != nr[k])
    chain_methods = set(a.chain_methods.split(","))
    residue_methods = set(a.residue_methods.split(","))
    explained = collections.Counter()
    unexplained = []
    for k in only_c:
        m = nr[k]["method"]
        if m not in chain_methods:
            explained["committed-only row from method %s (not in the declared chain)" % m] += 1
        else:
            unexplained.append({"addr": k, "kind": "committed_only", "row": nr[k]})
    for k in only_m:
        if nm[k]["method"] in residue_methods:
            explained["chain-only row from residue stage %s" % nm[k]["method"]] += 1
        else:
            unexplained.append({"addr": k, "kind": "chain_only", "row": nm[k]})
    for k in changed:
        unexplained.append({"addr": k, "kind": "changed", "chain": nm[k], "committed": nr[k]})
    res["names"] = {"commit": sha, "identical": mine == ref, "rows_chain": len(nm), "rows_committed": len(nr),
                    "committed_methods": dict(sorted(collections.Counter(r["method"] for r in nr.values()).items())),
                    "chain_methods": dict(sorted(collections.Counter(r["method"] for r in nm.values()).items())),
                    "explained_differences": dict(explained), "unexplained": len(unexplained),
                    "unexplained_examples": unexplained[:10]}
    ok &= mine == ref or not unexplained

    if not a.skip_run:
        p2, t2 = run(a.config)
        (out / "regress_run2.log").write_text(p2.stdout + p2.stderr, encoding="utf-8")
        line = next((l for l in p2.stdout.splitlines() if l.startswith("files changed:")), "")
        res["run2_idempotence"] = {"exit": p2.returncode, "seconds": round(t2, 1), "line": line,
                                   "zero_files_changed": line.startswith("files changed: 0")}
        ok &= p2.returncode == 0 and res["run2_idempotence"]["zero_files_changed"]
    res["pass"] = bool(ok)
    print(json.dumps(res, indent=1))
    print("REGRESS_DS2 %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
