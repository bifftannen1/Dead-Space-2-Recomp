"""Resolve a ds2_crash_N.txt stack (DS2_CRASHDUMP builds) to function names via the linker map.

usage: python tools/symbolize_crash.py <ds2.map> <ds2_crash_N.txt>
"""
import bisect, re, sys
mp, txt = sys.argv[1], sys.argv[2]
syms = []
for l in open(mp, errors="replace"):
    p = l.split()
    if len(p) >= 3 and re.fullmatch(r"[0-9a-f]{16}", p[2]) and p[0].startswith("0001:"):
        syms.append((int(p[2], 16) - 0x140000000, p[1]))
syms.sort(); keys = [s[0] for s in syms]
for l in open(txt):
    m = re.search(r"ds2\.exe\+0x([0-9A-F]+)", l)
    if m:
        r = int(m.group(1), 16); i = bisect.bisect_right(keys, r) - 1
        print(l.rstrip(), "  ", syms[i][1], f"+0x{r - syms[i][0]:X}")
    else:
        print(l.rstrip())
