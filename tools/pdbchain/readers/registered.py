"""Recompiler function-list adapters.

Every adapter returns Registered(funcs=[(addr, symbol)] sorted, calls={addr: set(neighbour addrs)},
callees={addr: set(callee addrs)}, source=str). Extent of a function = distance to the next
registered start (padding included), which is what every DS2 measurement used.

  rexglue  ReXGlue codegen output dir: <prefix>_register.cpp (SetFunction(0xADDR, sym);) and
           <prefix>_recomp.*.cpp (DEFINE_REX_FUNC(sym) bodies calling sym(ctx, base)).
  tsv      generic: columns addr [symbol] [size]; optional calls TSV (caller, callee). Without a
           calls table the call graph is decoded from direct bl targets in the retail image.
"""
from __future__ import annotations

import collections, re
from dataclasses import dataclass, field
from pathlib import Path

from ..util import tsv


@dataclass
class Registered:
    funcs: list
    calls: dict = field(default_factory=dict)
    callees: dict = field(default_factory=dict)
    source: str = ""
    files: list = field(default_factory=list)


def rexglue(gen_dir: str, prefix: str | None = None) -> Registered:
    gen = Path(gen_dir)
    regs = sorted(gen.glob("%s_register.cpp" % prefix if prefix else "*_register.cpp"))
    if len(regs) != 1:
        raise SystemExit("REFUSED: expected exactly one *_register.cpp in %s, found %d" % (gen, len(regs)))
    reg = regs[0]
    prefix = reg.name[:-len("_register.cpp")]
    text = reg.read_text(encoding="utf-8")
    funcs = [(int(a, 16), s) for a, s in re.findall(r"SetFunction\(0x([0-9A-Fa-f]+), (\w+)\);", text)]
    if len({a for a, _ in funcs}) != len(funcs):
        raise SystemExit("REFUSED: duplicate address in %s" % reg)
    funcs.sort()
    calls, callees = collections.defaultdict(set), collections.defaultdict(set)
    call_re = re.compile(r"\b(sub_[0-9A-F]{8})\(ctx, base\)")
    sym2addr = {s: x for x, s in funcs}
    files = sorted(gen.glob("%s_recomp.*.cpp" % prefix))
    for p in files:
        cur = None
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("DEFINE_REX_FUNC("):
                cur = sym2addr.get(line[16:line.index(")")])
            elif cur is not None:
                for m in call_re.finditer(line):
                    t = sym2addr.get(m.group(1))
                    if t is not None and t != cur:
                        calls[cur].add(t); calls[t].add(cur); callees[cur].add(t)
    return Registered(funcs, calls, callees, "rexglue:" + str(reg), [str(reg)] + [str(f) for f in files])


def generic_tsv(path: str, calls_path: str | None = None, rrd=None) -> Registered:
    funcs = []
    for r in tsv(path):
        a = int(r["addr"], 16)
        funcs.append((a, r.get("symbol") or "sub_%08X" % a))
    if len({a for a, _ in funcs}) != len(funcs):
        raise SystemExit("REFUSED: duplicate address in %s" % path)
    funcs.sort()
    calls, callees = collections.defaultdict(set), collections.defaultdict(set)
    addrs = {a for a, _ in funcs}
    if calls_path:
        for r in tsv(calls_path):
            c, t = int(r["caller"], 16), int(r["callee"], 16)
            if c in addrs and t in addrs and c != t:
                calls[c].add(t); calls[t].add(c); callees[c].add(t)
    elif rrd is not None:
        from ..arch import ppc
        xs = [a for a, _ in funcs]
        for x, nx in zip(xs, xs[1:] + [None]):
            b = rrd(x, (nx - x) if nx else 4)
            for t in ppc.bl_targets(b, x):
                if t in addrs and t != x:
                    calls[x].add(t); calls[t].add(x); callees[x].add(t)
    return Registered(funcs, calls, callees, "tsv:" + str(path), [str(path)] + ([calls_path] if calls_path else []))
