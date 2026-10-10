"""PDB source path -> named-tree path, driven by a rules TSV (pattern, replacement).

Normalisation: backslashes to '/', '.' and empty parts dropped, '..' pops (as DeSwitch's
deswitch_srcpaths.norm); lowercased; first matching rule wins, else 'other/'; every segment made
filesystem-safe ([^A-Za-z0-9_.+-] -> '_', 120 chars, as deswitch_srctree.safe); '.cpp' appended
unless the file already ends in .cpp ('ldo.c' -> 'ldo.c.cpp'); a trailing '.obj' (obj fallback)
is dropped first.
"""
from __future__ import annotations

import re
from pathlib import Path


def norm(p):
    p = p.replace("\\", "/")
    out = []
    for part in p.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if out:
                out.pop()
            continue
        out.append(part)
    return out


def safe(part):
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", part)[:120] or "_"


class TreePaths:
    def __init__(self, rules_tsv):
        self.rules = []
        if rules_tsv:
            for line in Path(rules_tsv).read_text(encoding="utf-8").splitlines():
                if not line or line.startswith("#") or line.startswith("pattern\t"):
                    continue
                pat, rep = line.split("\t")
                self.rules.append((re.compile(pat), rep))

    def tree_path(self, src: str) -> str:
        p = "/".join(norm(src)).lower() if src[:2] != ".." else "../" + "/".join(norm(src[3:])).lower()
        for pat, rep in self.rules:
            m = pat.match(p)
            if m:
                p = m.expand(rep) + p[m.end():]
                break
        else:
            p = "other/" + p
        parts = [safe(x) for x in p.split("/") if x]
        if parts[-1].endswith(".obj"):
            parts[-1] = parts[-1][:-4]
        if not parts[-1].endswith(".cpp"):
            parts[-1] += ".cpp"
        return "/".join(parts)


IDENT_BAD = re.compile(r"[^A-Za-z0-9_]")


def ident_for(name: str, addr: int) -> str:
    """C++-safe unique identifier ns__Class__method_<ADDR>."""
    s = IDENT_BAD.sub("_", name.replace("::", "__"))
    s = re.sub(r"_{3,}", "__", s).strip("_")[:160] or "fn"
    if s[0].isdigit():
        s = "_" + s
    return "%s_%08X" % (s, addr)
