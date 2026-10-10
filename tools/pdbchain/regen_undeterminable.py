r"""Regenerate the `inputs.undeterminable` list from the chain's current allocation.

    python tools/pdbchain/regen_undeterminable.py [--alloc build/pdbchain/ds2/allocation.tsv.gz]
                                                  [--list config/undeterminable.tsv] [--check]

A listed row is kept only while the chain's allocation still has it as UNDETERMINABLE with the
list basis; rows a later gated stage resolved (A8 NAME_MODULE) are dropped. Every kept row keeps
its refusal evidence unchanged (`evidence`, `refused_by`), and a kept row without evidence is a
refusal. A row the allocation holds as UNDETERMINABLE but the list lacks is a refusal too: the
list is evidence, never grown here (write it from a residue review).
Second run: "rows changed: 0". --check writes nothing and exits 1 when the list is stale.
Shake: the staleness test must flag a copy of the allocation with one kept row set to NAME_MODULE.
"""
from __future__ import annotations

import argparse, csv, gzip, io, sys
from pathlib import Path

csv.field_size_limit(1 << 30)
REPO = Path(__file__).resolve().parents[2]


def classes(alloc_rows):
    return {r["addr"]: r["class"] for r in alloc_rows}


def keep_rows(lst, cls):
    return [r for r in lst if cls.get(r["addr"]) == "UNDETERMINABLE"]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--alloc", default=str(REPO / "build/pdbchain/ds2/allocation.tsv.gz"))
    ap.add_argument("--list", default=str(REPO / "config/undeterminable.tsv"))
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    alloc = list(csv.DictReader(io.StringIO(gzip.decompress(Path(a.alloc).read_bytes()).decode("utf-8")),
                                delimiter="\t", quoting=csv.QUOTE_NONE))
    raw = Path(a.list).read_bytes().decode("utf-8")
    rd = csv.DictReader(io.StringIO(raw), delimiter="\t", quoting=csv.QUOTE_NONE)
    hdr, lst = rd.fieldnames, list(rd)
    cls = classes(alloc)
    keep = keep_rows(lst, cls)
    listed = {r["addr"] for r in lst}
    unlisted = sorted(x for x, c in cls.items() if c == "UNDETERMINABLE" and x not in listed)
    missing_ev = [r["addr"] for r in keep if not r["evidence"].strip() or not r["refused_by"].strip()]
    dropped = sorted({r["addr"]: cls.get(r["addr"]) for r in lst if r not in keep}.items())
    # shake: one kept row flipped to NAME_MODULE in a copy must be dropped
    shake = None
    if keep:
        c2 = dict(cls); c2[keep[0]["addr"]] = "NAME_MODULE"
        shake = len(keep_rows(lst, c2)) == len(keep) - 1
    print("listed %d -> kept %d; dropped %d (%s); unlisted UNDETERMINABLE %d; kept without evidence %d; shake %s"
          % (len(lst), len(keep), len(dropped),
             ", ".join("%s=%d" % (k, sum(1 for _, c in dropped if c == k)) for k in sorted({c or "absent" for _, c in dropped})),
             len(unlisted), len(missing_ev), shake))
    if unlisted or missing_ev or shake is False:
        print("REGEN_UNDETERMINABLE REFUSED (unlisted %s; no evidence %s)" % (unlisted[:5], missing_ev[:5]))
        return 2
    # rows are copied as their original lines (no re-quoting), so evidence stays byte-identical
    lines = raw.split("\n")
    kept = {r["addr"] for r in keep}
    text = "\n".join([lines[0]] + [ln for ln in lines[1:] if ln and ln.split("\t", 1)[0] in kept]) + "\n"
    changed = text != raw
    if a.check:
        print("stale" if changed else "current")
        return 1 if changed else 0
    if changed:
        Path(a.list).write_bytes(text.encode("utf-8"))
    print("rows changed: %d" % (len(lst) - len(keep) if changed else 0))
    print("REGEN_UNDETERMINABLE DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
