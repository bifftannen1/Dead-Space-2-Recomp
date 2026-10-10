r"""deswitch_srcpaths.py -- reconstruct a project's ORIGINAL source tree from the
file paths a shipped build left behind.

  A shipped binary keeps a source path only where a macro wrote one in --
  `__FILE__` in an assert, or a debug record the linker emitted.  Those strings
  are the developers' own directory layout, and nothing else in a stripped
  binary is.  This tool merges them; it does not guess a layout.

EVIDENCE CLASSES, IN ORDER OF AUTHORITY
  1 stabs_N_SO         Mach-O STABS: a compile DIRECTORY followed by a FILE.
                       The only fully-qualified source layout of the three.
  2 rooted_build_path  `X:\...\<root>\...\File.cpp` from __FILE__/assert text.
                       Rooted, so it fixes where the tree begins.
  3 relative_fragment  `.\prog/game/foo.inl`, `lib\tsl/array.h`.  Real, but
                       locatable only once 1 or 2 has supplied a root.

  The order is not a ranking of trust -- every class is a literal string from
  the image.  It decides who wins a CONFLICT, and the classes are authoritative
  over different things:

    * class 2 fixes the ROOT.  Class 3 has none.
    * class 3 often preserves the CASE that class 2 lost.  MSVC lowercases
      __FILE__ for some translation units, so the same file appears as
      `game\prog\game\chara\player\pl_damage.inl` (rooted, lowercased) and
      `.\prog/game/chara/player/pl_damage.inl` (fragment, real case).  Taking
      the rooted spelling wholesale yields a tree whose casing is wrong in
      exactly the places the compiler happened to normalise.

  So case is canonicalised per DIRECTORY, keyed by the full lowercase PREFIX --
  never by the bare component name.  `Game\prog\game\` holds `game` at two
  depths as two different directories; a per-component map collapses them and
  silently renames one.

  An `N_OSO` entry is NOT source.  It names the OBJECT file
  (`Source/Intermediate/<proj>/Shipping/<Module>/<File>.cpp.o`) -- the build
  output tree.  It sits under `Source/` too, so counting it as source inflates
  the layout with a directory nobody ever edited in.  Excluded, and counted
  separately so the exclusion is visible.

WHAT IT DOES NOT DO
  A path proves a file existed and where it sat.  It says nothing about the
  contents, and no contents are invented: directories are created and a
  manifest is written recording which evidence class placed each file.
"""
from __future__ import annotations
import argparse, io, json, re, struct, sys
from collections import Counter
from pathlib import Path

SRC_EXT = ('.c', '.cc', '.cpp', '.cxx', '.h', '.hh', '.hpp', '.hxx', '.inl',
           '.m', '.mm', '.asm', '.s', '.uc', '.hlsl', '.fx', '.fxh', '.cs',
           '.usf', '.ush', '.java', '.py')

# precedence: lower number wins a conflict
CLASS = {'stabs_N_SO': 1, 'rooted_build_path': 2, 'relative_fragment': 3}


# ----------------------------------------------------------------- readers
def macho_stabs(path):
    """(dir, file) pairs from N_SO; N_OSO strings returned separately."""
    raw = path.read_bytes()
    magic = struct.unpack_from('<I', raw, 0)[0]
    if magic not in (0xfeedface, 0xfeedfacf):
        return [], []
    wide = magic == 0xfeedfacf
    ncmds = struct.unpack_from('<I', raw, 16)[0]
    p, sym = (32 if wide else 28), None
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', raw, p)
        if cmd == 0x2:
            sym = struct.unpack_from('<IIII', raw, p + 8)
            break
        p += cmdsize
    if not sym:
        return [], []
    symoff, nsyms, stroff, _ = sym
    esize = 16 if wide else 12
    so, oso, cur = [], [], None
    for i in range(nsyms):
        e = symoff + i * esize
        if e + esize > len(raw):
            break
        strx = struct.unpack_from('<I', raw, e)[0]
        nt = raw[e + 4]
        if nt not in (0x64, 0x66):
            continue
        s = raw[stroff + strx:raw.find(b'\0', stroff + strx)].decode('latin1', 'replace')
        if nt == 0x64:
            if not s:
                cur = None
            elif s.endswith('/'):
                cur = s
            elif cur:
                so.append(cur + s)
        elif s:
            oso.append(s)
    return so, oso


def read_pathmine_json(p):
    """deswitch_pathmine --output JSON -> [(path, lane)]"""
    d = json.loads(Path(p).read_text(encoding='utf-8'))
    out = []
    for inp in d.get('inputs', []):
        for path, info in (inp.get('detail') or {}).items():
            out.append((path, info.get('lane', 'relative')))
    return out


def read_paths_tsv(p):
    out = []
    for line in io.open(p, encoding='utf-8'):
        f = line.rstrip('\n').split('\t')
        if len(f) >= 5 and f[3] in ('rooted', 'relative'):
            out.append((f[4], f[3]))
    return out


# ----------------------------------------------------------------- paths
def norm(p):
    p = p.replace('\\', '/')
    out = []
    for part in p.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if out:
                out.pop()
            continue
        out.append(part)
    return out


def cut_at_root(parts, marker):
    """Trim to begin at the FIRST component equal to the root marker.

    First, not last: build-machine prefixes sit above the root, and a project
    frequently repeats the root's name further down (`Game/prog/game`).  Last
    occurrence cuts the tree off at that inner directory.
    """
    low = marker.lower()
    for i, c in enumerate(parts):
        if c.lower() == low:
            return parts[i:]
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--macho', type=Path, action='append', default=[])
    ap.add_argument('--pathmine-json', type=Path, action='append', default=[])
    ap.add_argument('--paths-tsv', type=Path, action='append', default=[])
    ap.add_argument('--root-marker', required=True,
                    help='top directory of the developers tree, e.g. Source, Game')
    ap.add_argument('--fragment-top', action='append', default=[],
                    help='a top-level dir under the root that relative fragments '
                         'may be rooted at (repeatable). Without any, fragments '
                         'are only placed when their first component is already '
                         'known from rooted evidence.')
    ap.add_argument('--exclude', action='append', default=[],
                    help='drop paths containing this substring (case-insensitive)')
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--report', type=Path)
    ap.add_argument('--touch-files', action='store_true',
                    help='create zero-byte placeholders. Off by default: an empty '
                         '.cpp reads as recovered source, and none was recovered.')
    args = ap.parse_args()

    raw = []            # (parts, class)
    stats = Counter()
    obj_tree = set()

    for m in args.macho:
        so, oso = macho_stabs(m)
        stats['N_SO'] += len(so)
        stats['N_OSO (object tree, excluded)'] += len(oso)
        for s in so:
            raw.append((norm(s), 'stabs_N_SO'))
        for o in oso:
            c = cut_at_root(norm(o), args.root_marker)
            if c:
                obj_tree.add('/'.join(c))

    for src, rd in ((args.pathmine_json, read_pathmine_json),
                    (args.paths_tsv, read_paths_tsv)):
        for f in src:
            for path, lane in rd(f):
                raw.append((norm(path),
                            'rooted_build_path' if lane.startswith('rooted')
                            else 'relative_fragment'))

    ex = [e.lower() for e in args.exclude]
    kept = []
    for parts, klass in raw:
        if not parts or not parts[-1].lower().endswith(SRC_EXT):
            continue
        joined = '/'.join(parts).lower()
        if any(e in joined for e in ex):
            stats['excluded by --exclude'] += 1
            continue
        kept.append((parts, klass))
    stats['source-extension candidates'] = len(kept)

    # -- pass 1: everything that can be rooted directly ------------------
    rooted = []
    unrooted = []
    for parts, klass in kept:
        # Only an ABSOLUTE path can establish where the root is.  A fragment
        # such as `.\prog/game/utility/Foo.h` contains the marker at an inner
        # position; rooting there silently discards the `prog/game/` above it.
        c = cut_at_root(parts, args.root_marker)             if klass != 'relative_fragment' else None
        if c and len(c) > 1:
            rooted.append((c, klass))
        else:
            unrooted.append((parts, klass))

    tops = {p[1].lower() for p, _ in rooted if len(p) > 2}
    tops |= {t.lower() for t in args.fragment_top}

    # -- pass 2: place fragments beneath the established root ------------
    placed_frag = 0
    unplaced = []
    rootedset = {'/'.join(r).lower() for r, _ in rooted}
    # Reference tree = what pass 1 established from ROOTED evidence only.
    # Fragments are matched against that fixed tree, never against each
    # other: letting a fragment land on a location another fragment just
    # invented would compound one guess into a branch of the layout.
    ref_dirs = {'/'.join(r[:-1]).lower() for r, _ in rooted if len(r) > 1}
    base_count = Counter(r[-1].lower() for r, _ in rooted)
    base_dir = {}
    for r, _ in rooted:
        base_dir.setdefault(r[-1].lower(), '/'.join(r[:-1]))

    for parts, klass in unrooted:
        # (a) The same __FILE__ appears both drive-qualified and without
        # the drive, so a fragment is often a copy of a path already
        # placed.  Cut it at the root and ask whether it is already there;
        # counting these as misses overstates what was not recovered (60
        # of 62 on one title).  Safe for a genuine fragment such as
        # `prog/game/utility/Foo.h`: cutting THAT at its inner `game`
        # gives `game/utility/Foo.h`, which is not placed, so it falls
        # through and gets rooted beneath the tree below instead.
        cut = cut_at_root(parts, args.root_marker)
        if cut and '/'.join(cut).lower() in rootedset:
            stats['fragment duplicates a rooted path'] += 1
            continue

        if len(parts) == 1:
            # (b) A bare basename carries no location, so it can never
            # place a file.  It is only useful as confirmation of one
            # already placed -- and only when the basename is unique,
            # since two directories may hold the same filename.
            n = parts[0].lower()
            if base_count.get(n) == 1:
                stats['bare basename confirms a placed file'] += 1
            else:
                unplaced.append('/'.join(parts))
            continue

        # (c) Unique-suffix join.  The fragment is relative to some deeper
        # directory, so match its leading directory components against
        # directories in the reference tree.  Try the LONGEST prefix
        # first and shorten until one resolves: a header directory such
        # as `Engine/Inc/Lighting` may never appear in the tree at all,
        # because headers are not compile units and only .cpp files leave
        # a compile directory behind -- but `Engine` does appear, and the
        # unmatched tail below it is then carried over intact.
        #
        # Every length requires EXACTLY one match.  Two candidates mean
        # the evidence does not say which, and picking either would put a
        # guess in the tree wearing the same label as a recovered path.
        dparts = [c.lower() for c in parts[:-1]]
        hit = None
        for k in range(len(dparts), 0, -1):
            pre = '/'.join(dparts[:k])
            cands = [d for d in ref_dirs
                     if d == pre or d.endswith('/' + pre)]
            if len(cands) == 1:
                hit = cands[0].split('/') + list(parts[k:-1])
                break
            if len(cands) > 1:
                stats['fragment prefix ambiguous, not placed'] += 1
                hit = False
                break
        if hit:
            rooted.append((hit + [parts[-1]], 'relative_fragment'))
            placed_frag += 1
            stats['fragment located by unique suffix'] += 1
            continue
        if hit is False:
            unplaced.append('/'.join(parts))
            continue

        # (d) Last resort: a fragment starting at a known top-level
        # directory of the tree is relative to the root itself.
        if parts[0].lower() in tops:
            rooted.append(([args.root_marker] + parts, 'relative_fragment'))
            placed_frag += 1
            stats['fragment rooted at a top-level dir'] += 1
        else:
            unplaced.append('/'.join(parts))

    stats['fragments placed'] = placed_frag
    stats['fragment NOT placed'] = len(unplaced)

    # -- pass 3: canonical case per directory, keyed by full prefix -------
    #    (a per-component map would merge Game/ and prog/game/)
    best = {}
    for parts, klass in sorted(rooted, key=lambda x: CLASS[x[1]]):
        pref = []
        for comp in parts:
            key = ('/'.join(pref) + '/' + comp).lower()
            cur = best.get(key)
            if cur is None:
                best[key] = (CLASS[klass], comp)
            else:
                rank, spell = cur
                mine = CLASS[klass]
                # better class wins; then a spelling carrying real case wins
                if mine < rank or (mine == rank and
                                   spell.islower() and not comp.islower()):
                    best[key] = (mine, comp)
            pref.append(comp)

    def canon(parts):
        out, pref = [], []
        for comp in parts:
            key = ('/'.join(pref) + '/' + comp).lower()
            c = best.get(key, (99, comp))[1]
            out.append(c)
            pref.append(comp)
        return out

    files = {}
    for parts, klass in rooted:
        p = '/'.join(canon(parts))
        if p not in files or CLASS[klass] < CLASS[files[p]]:
            files[p] = klass

    print('evidence read: %s' % dict(stats))
    print('object-tree entries excluded (N_OSO): %d' % len(obj_tree))
    print('SOURCE files placed: %d' % len(files))
    for k, v in Counter(files.values()).most_common():
        print('    %-20s %d' % (k, v))

    root = args.output
    root.mkdir(parents=True, exist_ok=True)
    dirs = set()
    for f in files:
        d = root / Path(f).parent
        d.mkdir(parents=True, exist_ok=True)
        dirs.add(str(Path(f).parent).replace('\\', '/'))
        if args.touch_files:
            t = root / f
            if not t.exists():
                t.write_text('', encoding='utf-8')
    print('directories created: %d' % len(dirs))

    if unplaced:
        print('\nnot placed (no root, and no known top-level dir): %d'
              % len(unplaced))
        for u in sorted(unplaced)[:20]:
            print('   %s' % u)
        (root / '_UNPLACED.txt').write_text(
            '\n'.join(sorted(unplaced)) + '\n', encoding='utf-8')

    with (root / '_MANIFEST.tsv').open('w', encoding='utf-8') as fh:
        fh.write('source_path\tevidence_class\n')
        for f, ev in sorted(files.items()):
            fh.write('%s\t%s\n' % (f, ev))

    top = Counter('/'.join(f.split('/')[:-1]) for f in files)
    print('\ndirectories, by file count:')
    for k, v in top.most_common(30):
        print('   %-58s %3d' % (k, v))

    if args.report:
        args.report.write_text(json.dumps(
            {'schema': 'deswitch-srcpaths-v2',
             'root_marker': args.root_marker,
             'source_files': len(files), 'directories': len(dirs),
             'by_evidence': dict(Counter(files.values())),
             'object_tree_entries_excluded': len(obj_tree),
             'evidence_counts': dict(stats),
             'unplaced': sorted(unplaced),
             'directories_by_count': dict(top.most_common(200)),
             'holds': [
                 'every path is a literal string from the image; none inferred',
                 'a path proves a file existed and where; nothing about contents',
                 'N_OSO is the object tree and is excluded from source',
                 'case canonicalised per directory keyed by full prefix',
             ]}, indent=1) + '\n', encoding='utf-8')
        print('wrote %s' % args.report)
    return 0


if __name__ == '__main__':
    sys.exit(main())
