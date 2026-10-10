"""Probe: pdbchain PDB-table extraction vs the reference build/naming tables (f/q/d).

    python tools/pdbchain/probe_pdb_tables.py
Prints per table: rows ref/chain, rows equal as multisets, first differences. Exit 1 on any delta.
Positive control: a copy of the chain table with one row's obj altered must be reported unequal.
"""
import collections, gzip, os, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
from pdbchain.readers import pdb as PDB  # noqa: E402

# Set these for your machine: PDB reader dir, prototype PEs, prototype PDBs (%s = f/q/d).
LANES = os.environ.get("PDBCHAIN_READER_DIR", "xb360_toolkit/lanes")
PE = os.environ.get("PDBCHAIN_PE", "pe/ds2_%s.pe")
PDBP = os.environ.get("PDBCHAIN_PDB", "pdb/deadspace_%s.pdb")
REF = {"procs": "pdb_procs%s", "contribs": "pdb_contribs%s", "dcontribs": "pdb_dcontribs%s", "thunks": "pdb_thunks%s"}


def ref_rows(name):
    with gzip.open(REPO / "build" / "naming" / (name + ".tsv.gz"), "rt", encoding="utf-8") as f:
        lines = f.read().splitlines()
    return [tuple(l.split("\t")) for l in lines[1:]]


def cmp(a, b):
    ca, cb = collections.Counter(a), collections.Counter(b)
    return ca == cb, list((ca - cb).elements())[:5], list((cb - ca).elements())[:5], sum((ca - cb).values()), sum((cb - ca).values())


bad = 0
for lab in ("f", "q", "d"):
    t = PDB.extract_tables(LANES, PE % lab, PDBP % lab)
    suf = "" if lab == "f" else "_" + lab
    for k, pat in REF.items():
        r = ref_rows(pat % suf)
        c = [tuple(x) for x in t[k]]
        eq, only_ref, only_chain, nr, nc = cmp(r, c)
        print("%s %-9s ref %7d chain %7d equal %s  ref-only %d chain-only %d" % (lab, k, len(r), len(c), eq, nr, nc))
        if not eq:
            bad += 1
            for x in only_ref:
                print("   ref  ", x)
            for x in only_chain:
                print("   chain", x)
        if lab == "f" and k == "procs":
            c2 = list(c)
            c2[0] = c2[0][:5] + ("PERTURBED",) + c2[0][6:]
            print("   positive control (perturbed copy detected):", not cmp(r, c2)[0])
print("PROBE %s" % ("PASS" if not bad else "FAIL %d tables differ" % bad))
sys.exit(1 if bad else 0)
