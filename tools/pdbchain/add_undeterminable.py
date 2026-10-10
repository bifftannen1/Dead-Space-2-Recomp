r"""Append the chain's unresolved pending rows (work/pending_left.txt) to the undeterminable list,
each with its refusal evidence. Used when a stage is demoted by its gate: the rows it used to
allocate become UNDETERMINABLE and must be listed with why.

    python tools/pdbchain/add_undeterminable.py --pending build/pdbchain/dev/pending_left.txt
        --prev-alloc <allocation.tsv.gz before the demotion> --refused-by "<measured refusal>"
        [--list config/undeterminable.tsv]

Every appended row: size (registered extent), pattern "code", audit_class "demoted",
candidate_owners = the obj the demoted stage had chosen (a candidate, not an allocation),
evidence = the demoted stage's basis on that row, refused_by = the common refusal chain of
the list + the given measured refusal. Refuses when a pending address is already listed, or
was not allocated in --prev-alloc (then it is not a demotion and needs a residue review).
Second run: "rows added: 0".
"""
from __future__ import annotations

import argparse, csv, gzip, io, sys
from pathlib import Path

csv.field_size_limit(1 << 30)
REPO = Path(__file__).resolve().parents[2]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pending", required=True)
    ap.add_argument("--prev-alloc", required=True)
    ap.add_argument("--refused-by", required=True)
    ap.add_argument("--list", default=str(REPO / "config/undeterminable.tsv"))
    a = ap.parse_args(argv)
    raw = Path(a.list).read_text(encoding="utf-8")
    lines = raw.rstrip("\n").split("\n")
    listed = {ln.split("\t", 1)[0] for ln in lines[1:]}
    common = lines[1].split("\t")[6] if len(lines) > 1 else ""
    prev = list(csv.DictReader(io.StringIO(gzip.decompress(Path(a.prev_alloc).read_bytes()).decode("utf-8")),
                               delimiter="\t", quoting=csv.QUOTE_NONE))
    addrs = [int(r["addr"], 16) for r in prev]
    nxt = dict(zip(addrs, addrs[1:]))
    P = {r["addr"]: r for r in prev}
    pend = [x.strip() for x in Path(a.pending).read_text(encoding="utf-8").split() if x.strip()]
    add, bad = [], []
    for x in pend:
        r = P.get(x)
        if x in listed:
            continue
        if r is None or not r["obj"]:
            bad.append(x)
            continue
        v = int(x, 16)
        size = nxt.get(v, v + 4) - v
        add.append("\t".join([x, str(size), "code", "demoted", r["obj"] + " (former %s choice)" % r["class"],
                              "former_basis=%s;former_class=%s" % (r["basis"], r["class"]),
                              (common + "; " if common else "") + a.refused_by]))
    if bad:
        print("ADD_UNDETERMINABLE REFUSED: not a demotion (unallocated before): %s" % bad[:10])
        return 2
    if add:
        Path(a.list).write_text(raw.rstrip("\n") + "\n" + "\n".join(add) + "\n", encoding="utf-8")
        # keep address order
        ls = Path(a.list).read_text(encoding="utf-8").rstrip("\n").split("\n")
        Path(a.list).write_text("\n".join([ls[0]] + sorted(ls[1:], key=lambda s_: int(s_.split("\t", 1)[0], 16))) + "\n",
                                encoding="utf-8")
    print("rows added: %d" % len(add))
    print("ADD_UNDETERMINABLE DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
