r"""deswitch_srctree.py -- reconstruct a class/namespace TREE from a symbol table.

READ THIS BEFORE USING THE OUTPUT
  This does NOT recover source code.  A shipped binary contains compiled
  machine code; the bodies, local names, types, comments and file layout were
  destroyed by the compiler and are not in the file to be recovered.

  What a mangled C++ symbol does carry is an identity: enclosing namespaces,
  the class, the member name, and the parameter types.  That is real data, and
  rendering it as a declaration is a faithful transcription of it -- nothing is
  invented.  Every emitted line is traceable to a symbol and an address.

  So the output is a STRUCTURE MAP, not source:
    * declarations only, never bodies
    * the NAMESPACE tree, which is the code's own organisation -- NOT the
      developers' directory tree.  Their file layout is recoverable only if the
      binary kept __FILE__ paths, and many titles keep none.  Where those paths
      do exist, use them instead; this tool will not guess a layout.

  Do not represent the result as the original source of anything.
"""
from __future__ import annotations
import argparse, io, json, re, sys
from collections import defaultdict
from pathlib import Path

try:
    import itanium_demangler as itd
except ImportError:
    raise SystemExit('FAIL needs itanium_demangler (pip install itanium_demangler)')

BANNER = """// ============================================================
// RECONSTRUCTED FROM THE SYMBOL TABLE -- NOT ORIGINAL SOURCE
//
// Declarations transcribed from mangled symbols in:
//   {image}
// Bodies do not exist in the binary and are not reconstructed.
// Each entry carries the address the symbol resolves to.
// The path of this file reflects the NAMESPACE, not the
// developers' directory layout, which the binary does not record.
// ============================================================
"""

SKIP_SCOPE = re.compile(r'^(vtable for|typeinfo( name)? for|VTT for|'
                        r'construction vtable for|non-virtual thunk|guard variable)')


def load(path):
    out = []
    for line in io.open(path, encoding='utf-8'):
        p = line.split(None, 2)
        if len(p) == 3 and p[0] == 'L':
            out.append((int(p[1], 16), p[2].strip()))
    return out


def safe(part):
    return re.sub(r'[^A-Za-z0-9_.+-]', '_', part)[:120] or '_'


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('symbols', type=Path, help='"L 0xADDR name" lines')
    ap.add_argument('--output', type=Path, required=True, help='tree root')
    ap.add_argument('--image', default='(unspecified image)')
    ap.add_argument('--report', type=Path)
    ap.add_argument('--min-members', type=int, default=1)
    args = ap.parse_args()

    rows = load(args.symbols)
    print('symbols: %d' % len(rows))

    by_scope = defaultdict(list)
    demangled = failed = plain = 0
    for va, m in rows:
        name = m[1:] if m.startswith('__Z') else m
        if not name.startswith('_Z'):
            plain += 1
            by_scope['_C'].append((va, name, None))
            continue
        try:
            node = itd.parse(name)
        except Exception:
            node = None
        if node is None:
            failed += 1
            continue
        demangled += 1
        s = str(node)
        if SKIP_SCOPE.match(s):
            # vtables and typeinfo describe a class but declare no member
            continue
        base = re.sub(r'\(.*$', '', s)
        parts = base.split('::')
        scope = parts[:-1] or ['_global']
        by_scope['::'.join(scope)].append((va, s, parts[-1]))
    print('demangled %d, plain C %d, unparsed %d' % (demangled, plain, failed))
    print('scopes: %d' % len(by_scope))

    root = args.output
    root.mkdir(parents=True, exist_ok=True)
    written = members = 0
    index = {}
    for scope, items in sorted(by_scope.items()):
        if len(items) < args.min_members:
            continue
        parts = [safe(p) for p in scope.split('::')]
        d = root.joinpath(*parts[:-1]) if len(parts) > 1 else root
        d.mkdir(parents=True, exist_ok=True)
        f = d / (parts[-1] + '.h')
        lines = [BANNER.format(image=args.image),
                 '// scope: %s   members: %d' % (scope, len(items)), '']
        ns = scope.split('::')
        for i, n in enumerate(ns):
            if re.match(r'^[A-Za-z_]\w*$', n) and i < len(ns) - 1:
                lines.append('namespace %s {' % n)
        last = ns[-1]
        opened = re.match(r'^[A-Za-z_]\w*$', last) is not None
        if opened:
            lines.append('class %s {   // or namespace; the symbol does not say'
                         % last)
            lines.append('public:')
        for va, full, member in sorted(items, key=lambda x: x[0]):
            decl = full[len(scope) + 2:] if full.startswith(scope + '::') else full
            lines.append('    %-96s // 0x%08X' % (decl + ';', va))
            members += 1
        if opened:
            lines.append('};')
        for i, n in enumerate(ns):
            if re.match(r'^[A-Za-z_]\w*$', n) and i < len(ns) - 1:
                lines.append('}  // namespace %s' % n)
        f.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        index[scope] = {'file': str(f.relative_to(root)), 'members': len(items)}
        written += 1

    print('wrote %d files, %d declarations -> %s' % (written, members, root))
    if args.report:
        args.report.write_text(json.dumps(
            {'schema': 'deswitch-srctree-v1', 'image': args.image,
             'symbols': len(rows), 'demangled': demangled,
             'unparsed': failed, 'scopes': len(by_scope),
             'files': written, 'declarations': members,
             'holds': ['declarations transcribed from symbols; no bodies exist',
                       'namespace tree, not the developers file layout',
                       'not original source'],
             'index': index}, indent=1) + '\n', encoding='utf-8')
        print('wrote %s' % args.report)
    return 0


if __name__ == '__main__':
    sys.exit(main())
