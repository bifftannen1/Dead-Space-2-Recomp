r"""A/B two pdbchain output dirs: allocation, owners and names, row by row.

    python tools/pdbchain/ab_compare.py <dirA> <dirB>

Prints per table: identical or the differing rows (changed / A-only / B-only) counted by
class (allocation) or method (names), plus up to 10 examples. Exit 0 always (it measures).
Self-shake: a copy of A's names with one row dropped must show as 1 A-only row.
"""
from __future__ import annotations

import collections, csv, gzip, io, json, sys
from pathlib import Path

csv.field_size_limit(1 << 30)


def load(p):
    t = gzip.decompress(Path(p).read_bytes()).decode("utf-8")
    return list(csv.DictReader(io.StringIO(t), delimiter="\t", quoting=csv.QUOTE_NONE))


def diff(a, b, key, by):
    ia = {r[key]: r for r in a}
    ib = {r[key]: r for r in b}
    out = {"rows_a": len(a), "rows_b": len(b), "changed": collections.Counter(), "a_only": collections.Counter(),
           "b_only": collections.Counter(), "examples": []}
    for k in sorted(set(ia) | set(ib)):
        ra, rb = ia.get(k), ib.get(k)
        if ra == rb:
            continue
        if ra is None:
            out["b_only"][rb[by]] += 1
        elif rb is None:
            out["a_only"][ra[by]] += 1
        else:
            out["changed"]["%s->%s" % (ra[by], rb[by])] += 1
        if len(out["examples"]) < 10:
            out["examples"].append({"key": k, "a": ra and {by: ra[by], "name": ra.get("pdb_name"), "obj": ra.get("obj")},
                                    "b": rb and {by: rb[by], "name": rb.get("pdb_name"), "obj": rb.get("obj")}})
    out["identical"] = not (out["changed"] or out["a_only"] or out["b_only"])
    return out


def main(argv=None):
    A, B = map(Path, (argv or sys.argv[1:])[:2])
    res = {}
    for t, key, by in (("allocation.tsv.gz", "addr", "class"), ("names.tsv.gz", "addr", "method")):
        res[t] = diff(load(A / t), load(B / t), key, by)
    ow_a, ow_b = gzip.decompress((A / "allocation_owners.tsv.gz").read_bytes()), gzip.decompress((B / "allocation_owners.tsv.gz").read_bytes())
    res["allocation_owners.tsv.gz"] = {"identical": ow_a == ow_b}
    na = load(A / "names.tsv.gz")
    sh = diff(na, na[1:], "addr", "method")
    res["shake_dropped_row_detected"] = sum(sh["a_only"].values()) == 1
    print(json.dumps(res, indent=1, default=dict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
