"""Xenon (PowerPC, big endian) instruction decoding and masking.

Everything architecture-specific the chain uses lives here, so a port to another ISA replaces
this one module. The routines are the ones measured on Dead Space 2 (R1-13 evidence map, the
allocation cascade and the naming pass), kept bit-for-bit:

  relocation masks     b/bl (18), bc (16), lis/addi immediates (15/14), D-form displacements
  masked_sig           sha1 over masked words (index key for identical-code search)
  code_equal           evidence-map mask (D-form and DS-form displacements also masked)
  strict_equal_words   only branch displacements and lis/addi halves may differ
  operand_pairs        aligned bodies -> (call|ref, proto target, retail target)
  bl_targets / formed  direct-call targets; lis+D-form formed addresses
  tokens / shape_ops   normalised opcode streams for similarity
"""
from __future__ import annotations

import hashlib, struct

NAME = "xenon-ppc"
WORD = 4
# masks applied by masked_sig / strict comparisons (b/bl, bc, lis, addi)
MASK = {18: 0xFC000003, 16: 0xFFFF0003, 15: 0xFFFF0000, 14: 0xFFFF0000}
DFORM = set(range(32, 56)) | {14, 48, 50, 52, 54}
BLR, BCTR = 0x4E800020, 0x4E800420


def word(b: bytes, k: int) -> int:
    return int.from_bytes(b[k:k + 4], "big")


def sx16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def branch_disp(w: int) -> int:
    d = w & 0x03FFFFFC
    return d - 0x04000000 if d & 0x02000000 else d


def is_bl(w: int) -> bool:
    return w >> 26 == 18 and (w & 3) == 1


def masked_sig(b: bytes) -> str:
    h = hashlib.sha1()
    for k in range(0, len(b) - 3, 4):
        w = word(b, k)
        h.update((w & MASK.get(w >> 26, 0xFFFFFFFF)).to_bytes(4, "big"))
    return h.hexdigest()


def evmap_mask(w: int) -> int:
    op = w >> 26
    if op == 18:
        return w & 0xFC000003
    if op == 16:
        return w & 0xFFFF0003
    if op in (14, 15, 58, 62) or op in DFORM:
        return w & 0xFFFF0000
    return w


def code_equal(a: bytes, b: bytes) -> bool:
    if len(a) != len(b) or len(a) % 4:
        return False
    n = len(a) // 4
    wa, wb = struct.unpack(">%dI" % n, a), struct.unpack(">%dI" % n, b)
    return all(x == y or evmap_mask(x) == evmap_mask(y) for x, y in zip(wa, wb))


def data_equal(a, b, ptr_delta, lo, hi):
    """Relocation-aware data compare: a pointer-shaped word pair must equal (u, u + delta of the
    contribution holding u). Pairs whose target has no first-pass delta are unverified; a block
    whose differing pointer words are all unverified is not equal."""
    if len(a) != len(b) or a[len(a) // 4 * 4:] != b[len(b) // 4 * 4:]:
        return False
    n = len(a) // 4
    wa, wb = struct.unpack(">%dI" % n, a[:4 * n]), struct.unpack(">%dI" % n, b[:4 * n])
    verified = unverified = 0
    for u, v in zip(wa, wb):
        if lo <= u < hi:
            d = ptr_delta(u)
            if d is None:
                if u != v:
                    if not (lo <= v < hi):
                        return False
                    unverified += 1
            elif v != u + d:
                return False
            else:
                verified += 1
        elif u != v:
            return False
    return not unverified or verified > 0 and unverified == 0


def operand_pairs(pb, rb, pa, ra):
    """Aligned (masked-identical) bodies -> [(kind, proto_target, retail_target)]."""
    if pb is None or rb is None or len(pb) != len(rb) or not code_equal(pb, rb):
        return None
    n = len(pb) // 4
    wp, wr = struct.unpack(">%dI" % n, pb), struct.unpack(">%dI" % n, rb)
    hp, hr, out = {}, {}, []
    for i, (x, y) in enumerate(zip(wp, wr)):
        op = x >> 26
        if op == 18 and not (x & 2):
            dx, dy = x & 0x03FFFFFC, y & 0x03FFFFFC
            dx -= (dx & 0x02000000) << 1
            dy -= (dy & 0x02000000) << 1
            out.append(("call", (pa + 4 * i + dx) & 0xFFFFFFFF, (ra + 4 * i + dy) & 0xFFFFFFFF))
            continue
        rd_, ra_ = (x >> 21) & 31, (x >> 16) & 31
        if op == 15 and ra_ == 0:
            hp[rd_], hr[rd_] = (x & 0xFFFF) << 16, (y & 0xFFFF) << 16
            continue
        if (op in DFORM or op in (58, 62)) and ra_ in hp and ra_ in hr:
            m = 0xFFFC if op in (58, 62) else 0xFFFF
            sx, sy = x & m, y & m
            sx -= (sx & 0x8000) << 1
            sy -= (sy & 0x8000) << 1
            out.append(("ref", (hp[ra_] + sx) & 0xFFFFFFFF, (hr[ra_] + sy) & 0xFFFFFFFF))
        if op not in (36, 37, 38, 39, 44, 45, 52, 53, 54, 55, 62) and rd_ in hp:
            hp.pop(rd_, None); hr.pop(rd_, None)
    return out


def strict_equal_words(pb: bytes, rb: bytes) -> bool:
    """Only branch displacements (b/bl/bc) and lis/addi immediates may differ; D-form
    displacements (struct offsets, constants) must be identical."""
    if len(pb) != len(rb) or len(pb) % 4:
        return False
    for k in range(0, len(pb), 4):
        wp, wr = word(pb, k), word(rb, k)
        if wp == wr:
            continue
        op = wp >> 26
        if op != wr >> 26:
            return False
        m = MASK.get(op)
        if m is None or wp & m != wr & m:
            return False
    return True


def formed_pairs(pb: bytes, rb: bytes):
    """Yield (proto_addr, retail_addr) formed by lis rD,hi ; addi/load/store rX,lo(rD), walking
    two aligned bodies together."""
    hp, hr = {}, {}
    for k in range(0, len(pb), 4):
        wp, wr = word(pb, k), word(rb, k)
        op, rd, ra = wp >> 26, (wp >> 21) & 31, (wp >> 16) & 31
        if op == 15 and ra == 0:
            hp[rd], hr[rd] = (wp & 0xFFFF) << 16, (wr & 0xFFFF) << 16
            continue
        if (op == 14 or 32 <= op <= 55) and ra in hp:
            yield ((hp[ra] + sx16(wp & 0xFFFF)) & 0xFFFFFFFF, (hr[ra] + sx16(wr & 0xFFFF)) & 0xFFFFFFFF)
        if op in (14, 15) or 32 <= op <= 47:
            hp.pop(rd, None), hr.pop(rd, None)


def formed(b):
    """Addresses formed by lis rD,hi ; addi/load/store rX,lo(rD) in one body."""
    out, hi = [], {}
    if not b:
        return out
    for k in range(0, len(b) - 3, 4):
        w = word(b, k)
        op, rd, ra = w >> 26, (w >> 21) & 31, (w >> 16) & 31
        if op == 15 and ra == 0:
            hi[rd] = (w & 0xFFFF) << 16
            continue
        if (op == 14 or 32 <= op <= 55) and ra in hi:
            out.append((hi[ra] + sx16(w & 0xFFFF)) & 0xFFFFFFFF)
        if op in (14, 15) or 32 <= op <= 47:
            hi.pop(rd, None)
    return out


def bl_targets(b, va):
    out = []
    if not b:
        return out
    for k in range(0, len(b) - 3, 4):
        w = word(b, k)
        if is_bl(w):
            out.append((va + k + branch_disp(w)) & 0xFFFFFFFF)
    return out


def bl_pairs(rb: bytes, pb: bytes, x: int, pva: int):
    """Yield (retail_target, proto_target) for positions where both bodies have a bl."""
    for k in range(0, len(rb) - 3, 4):
        wr, wp = word(rb, k), word(pb, k)
        if is_bl(wr) and is_bl(wp):
            yield ((x + k + ((wr & 0x03FFFFFC) ^ 0x02000000) - 0x02000000) & 0xFFFFFFFF,
                   (pva + k + ((wp & 0x03FFFFFC) ^ 0x02000000) - 0x02000000) & 0xFFFFFFFF)


def b_or_bl_pairs(pb: bytes, rb: bytes, pva: int, x: int):
    """Yield (k, proto_target, retail_target) for every non-absolute b/bl in the proto body."""
    for k in range(0, len(pb) - 3, 4):
        wp, wr = word(pb, k), word(rb, k)
        if wp >> 26 == 18 and not wp & 2:
            yield (k, (pva + k + ((wp & 0x03FFFFFC) ^ 0x02000000) - 0x02000000) & 0xFFFFFFFF,
                   (x + k + ((wr & 0x03FFFFFC) ^ 0x02000000) - 0x02000000) & 0xFFFFFFFF)


def is_lis(w: int) -> bool:
    return w >> 26 == 15 and (w >> 16) & 31 == 0


def shape_ops(b: bytes):
    """Coarse opcode stream (top 11 bits: primary opcode + rD/rS) for shape similarity."""
    return [(int.from_bytes(b[i:i + 4], "big") >> 21) for i in range(0, len(b) - 3, 4)]


def tokens(b):
    """Normalised tokens: primary opcode, extended opcode for 19/31/59/63, link/absolute bits of
    branches; registers and immediates dropped; trailing zero words trimmed."""
    if not b:
        return []
    n = len(b) // 4
    while n and b[4 * n - 4:4 * n] == b"\0\0\0\0":
        n -= 1
    out = []
    for k in range(n):
        w = word(b, 4 * k)
        op = w >> 26
        if op in (19, 31, 59, 63):
            out.append((op << 11) | ((w >> 1) & 0x3FF))
        elif op in (16, 18):
            out.append((op << 11) | (w & 3))
        else:
            out.append(op << 11)
    return out
