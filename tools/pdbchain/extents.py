"""X0 extent normalisation, shared by allocation, naming and the residue stages.

normalise() is the single implementation of the X0 rule (docs: README "X0 extent
normalisation"). It derives an extent table only; codegen and the registered list are untouched.
  pdata    : a registered start that is a .pdata begin takes the raw .pdata length (>= 4)
  tail_blr : a function ending in an unconditional `b` followed directly by a registered 4-byte
             `blr` with no .pdata entry absorbs it (+4); the blr row is TAIL_OF:<prev>
Allowlisted ReXGlue boundaries are never re-sized by `pdata`.
"""
from __future__ import annotations

from .arch import ppc

BLR = 0x4E800020


def _words(rd, va, n):
    b = rd(va, n) if n > 0 else None
    return [ppc.word(b, k) for k in range(0, len(b) - 3, 4)] if b else []


def _is_b(w):
    return w >> 26 == 18 and (w & 3) == 0


def pdata_lengths(image):
    if image is None:
        return {}
    try:
        return {p["va"]: p["size"] for p in image.pdata()}
    except Exception:
        return {}


def read_allowlist(path):
    allow = set()
    if path:
        for line in open(path, encoding="utf-8"):
            f = line.rstrip("\n").split("\t")
            if len(f) > 1 and f[1].startswith("0x"):
                allow.add(int(f[1], 16))
    return allow


def normalise(keys, size, rrd, pdlen, allow, normalisers=("pdata", "tail_blr")):
    """keys: sorted registered starts; size: registered extents. Returns (ext_size, tail_of, stats)."""
    ext = dict(size)
    tail_of, st = {}, {}
    for nm in normalisers:
        if nm == "pdata":
            k = 0
            for x in keys:
                if x in pdlen and pdlen[x] >= 4 and x not in allow:
                    k += ext[x] != pdlen[x]
                    ext[x] = pdlen[x]
            st["pdata_declared"] = sum(1 for x in keys if x in pdlen)
            st["pdata_extent_differs_from_registered_gap"] = k
        elif nm == "tail_blr":
            for i, x in enumerate(keys[:-1]):
                y = keys[i + 1]
                if size[y] == 4 and y == x + size[x] and _words(rrd, y, 4) == [BLR] and y not in pdlen:
                    lw = _words(rrd, y - 4, 4)
                    if lw and _is_b(lw[0]):
                        tail_of[y] = x
                        ext[x] = max(ext[x], size[x] + 4)
            st["lone_blr_after_tail_branch"] = len(tail_of)
        else:
            raise SystemExit("REFUSED: unknown extent normaliser %r" % nm)
    return ext, tail_of, st


def for_ctx(ctx, keys, size, normalisers=("pdata", "tail_blr")):
    """normalise() with the run's retail image (.pdata) and `inputs.extent_allowlist`."""
    pdlen = pdata_lengths(getattr(ctx, "retail_image", None))
    allow = read_allowlist(ctx.inputs.get("extent_allowlist"))
    return normalise(keys, size, ctx.rrd, pdlen, allow, normalisers)
