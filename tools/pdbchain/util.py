"""Shared helpers: TSV I/O, deterministic gzip, idempotent writes, hashing."""
from __future__ import annotations

import csv, gzip, hashlib, io, json
from pathlib import Path

csv.field_size_limit(1 << 30)


def tsv(path):
    """Rows of a tab-separated file (optionally .gz) as dicts. QUOTE_NONE: fields are raw."""
    p = str(path)
    op = gzip.open(p, "rt", encoding="utf-8") if p.endswith(".gz") else open(p, encoding="utf-8")
    with op as f:
        yield from csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE)


def sha256_file(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def gz_bytes(data: bytes) -> bytes:
    """gzip with mtime 0 and no file name, so equal content gives equal bytes."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, filename="") as g:
        g.write(data)
    return buf.getvalue()


class Writer:
    """Idempotent output writer: a file is written only when its bytes change; the count of
    changed files is the idempotence meter (a second run must report 0)."""

    def __init__(self):
        self.changed, self.unchanged = [], []

    def write(self, path, data: bytes, gz: bool = False) -> bool:
        p = Path(path)
        if gz:
            data = gz_bytes(data)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_file() and p.read_bytes() == data:
            self.unchanged.append(str(p))
            return False
        p.write_bytes(data)
        self.changed.append(str(p))
        return True

    def write_json(self, path, obj) -> bool:
        return self.write(path, (json.dumps(obj, indent=1, sort_keys=True) + "\n").encode("utf-8"))


def table_text(header, rows) -> bytes:
    return ("\t".join(header) + "\n" + "".join("\t".join(r) + "\n" for r in rows)).encode("utf-8")


def h32(x) -> str:
    return "" if x is None else "0x%08X" % x
