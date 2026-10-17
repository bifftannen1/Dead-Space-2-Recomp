"""DS2 TU2 alpha launcher. See SOURCES.md for the runtime contract."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import struct
from pathlib import Path, PurePosixPath
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
import settings_model

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from extract_xex import find_root, find_entry, SECTOR
from extract_dlc import Package

BASE = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1] / 'dist' / 'DS2-TU2-Alpha'
XEX_SHA = '928597ddb80240b8aefeef0a70a9476ee85d6de733035b326dd155c2094a3408'
XEXP_SHA = '8d386d37e8f71937b9d9c4b34bdbf85240101479c8aaece1002d94e7cdc01ec2'
DISC2_XEXP_SHA = 'dd5aaf4edba09cc332c75bb2027bfe308a077c52c411df2070fdf3193372fb5e'
REQUIRED_RUNTIME = {'ds2.exe', 'rexruntime.dll', 'rexgpu-xenos.dll'}

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, suffix='.tmp') as f:
        temp = Path(f.name)
        f.write(json.dumps(value, indent=2).encode('utf-8'))
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

def digest(data):
    return hashlib.sha256(data).hexdigest()

def read_xex(path):
    with Path(path).open('rb') as f:
        off, sec, size = find_root(f)
        sec, size = find_entry(f, off, sec, size, 'default.xex')
        # Both supported retail XEX files are exactly this size; measured input
        # receipts record it. Reject other lengths before allocating a buffer.
        if size != 14069760:
            raise ValueError('Disc executable length does not match the supported release')
        f.seek(off + sec * SECTOR)
        data = f.read(size)
    if len(data) != size or not data.startswith(b'XEX2'):
        raise ValueError('Disc image contains an incomplete or invalid default.xex')
    return data

def safe_paths(entries):
    """Validate every STFS path and parent chain before writing anything."""
    result = {}
    def resolve(i, seen):
        if i in seen or i < 0 or i >= len(entries) or entries[i] is None:
            raise ValueError('Invalid/cyclic STFS parent chain')
        e = entries[i]
        name = e['name']
        if (not name or name in ('.', '..') or name.rstrip(' .') != name
                or any(c in name for c in '/\\:<>"|?*') or any(ord(c)<32 for c in name)
                or name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',
                    *('COM'+str(n) for n in range(1,10)), *('LPT'+str(n) for n in range(1,10))}):
            raise ValueError('Unsafe STFS entry name')
        if e['parent'] == -1:
            return name
        parent = e['parent']
        if parent < 0 or parent >= len(entries) or not entries[parent] or not entries[parent]['dir']:
            raise ValueError('STFS parent is not a directory')
        return resolve(parent, seen | {i}) + '/' + name
    seen = set()
    for i, e in enumerate(entries):
        if e is None:
            continue
        p = resolve(i, set())
        if p.casefold() in seen:
            raise ValueError('Duplicate STFS output path')
        seen.add(p.casefold())
        result[i] = p
    return result

def payload(pkg, entry):
    data = b''.join(pkg.blocks(entry['start'], entry['blocks'], entry['contig']))[:entry['size']]
    if len(data) != entry['size']:
        raise ValueError('Incomplete STFS payload')
    return data

def patch_from_file(path, disc=1):
    path = Path(path)
    expected = XEXP_SHA if disc == 1 else DISC2_XEXP_SHA
    # An extracted patch is accepted only by the build's exact SHA-256 pin.
    if path.stat().st_size < 16 * 1024 * 1024:
        raw = path.read_bytes()
        if digest(raw) == expected:
            return raw
    pkg = Package(path)
    try:
        if pkg.title_id != 0x454108DF:
            raise ValueError('Update is not for Dead Space 2')
        entries = pkg.entries()
        paths = safe_paths(entries)
        for i, name in paths.items():
            if name.casefold() == f'disc{disc:03d}/default.xexp' and not entries[i]['dir']:
                data = payload(pkg, entries[i])
                if digest(data) != expected:
                    raise ValueError(f'Wrong title update or Media ID: Disc {disc} patch hash does not match TU2')
                return data
        raise ValueError(f'Update lacks disc{disc:03d}/default.xexp')
    finally:
        pkg.f.close()

def read_patch(path, disc=1):
    path = Path(path)
    if not zipfile.is_zipfile(path):
        return patch_from_file(path, disc)
    with zipfile.ZipFile(path) as z, tempfile.TemporaryDirectory() as temp:
        for i in z.infolist():
            if i.is_dir() or i.file_size > 16 * 1024 * 1024:
                continue
            # Flatten to an owned temporary filename: archive paths are never extracted.
            candidate = Path(temp) / 'candidate'
            candidate.write_bytes(z.read(i))
            try:
                return patch_from_file(candidate, disc)
            except (ValueError, SystemExit, OSError, IndexError, struct.error):
                continue
    raise ValueError(f'ZIP contains no matching TU2 Disc {disc} patch: use Title Update 2 (the "(v2)" zip), not v1')

STFS_MAGIC = (b'LIVE', b'PIRS', b'CON ')

def zip_member(path, kind):
    """Return the single disc image or STFS package inside a zip, else None for a plain file."""
    path = Path(path)
    if not zipfile.is_zipfile(path):
        return None
    with zipfile.ZipFile(path) as z:
        found = []
        for i in z.infolist():
            if i.is_dir():
                continue
            if kind == 'iso':
                if i.filename.lower().endswith('.iso'):
                    found.append(i)
            else:
                with z.open(i) as f:
                    if f.read(4) in STFS_MAGIC:
                        found.append(i)
    label = 'disc image (.iso)' if kind == 'iso' else 'Xbox 360 content package'
    if len(found) != 1:
        raise ValueError(f'{path.name}: expected exactly one {label} in the zip, found {len(found)}')
    return found[0]

def unpack(path, member, folder, log):
    """Stream one zip member to folder/<basename>; zipfile verifies its CRC as it reads."""
    name = PurePosixPath(member.filename).name
    safe_paths([dict(name=name, parent=-1, dir=False)])
    dest = Path(folder) / name
    if dest.is_file() and dest.stat().st_size == member.file_size:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(dest.parent).free < member.file_size + (512 << 20):
        raise ValueError(f'Not enough free space to unpack {name} ({member.file_size / 2**30:.1f} GB)')
    log(f'Unpacking {name} ({member.file_size / 2**30:.1f} GB) from {Path(path).name}; first launch only…')
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False, suffix='.part') as out:
        temp = Path(out.name)
        try:
            with zipfile.ZipFile(path) as z, z.open(member) as src:
                shutil.copyfileobj(src, out, 16 << 20)
        except BaseException:
            out.close()
            temp.unlink(missing_ok=True)
            raise
    os.replace(temp, dest)
    return dest

def disc_image(path, user, log):
    """A plain ISO is used in place; a zipped ISO is unpacked once and reused."""
    member = zip_member(path, 'iso')
    if member is None:
        return Path(path).resolve()
    folder = Path(user) / 'launcher-discs' / f'{Path(path).stem}-{member.CRC:08x}'
    return unpack(path, member, folder, log).resolve()

def install_dlc(path, user, log):
    member = zip_member(path, 'dlc')
    if member is None:
        return install_package(path, user, log)
    Path(user).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ds2-dlc-zip-', dir=Path(user)) as tmp:
        return install_package(unpack(path, member, tmp, log), user, log)

def install_package(path, user, log):
    pkg = Package(Path(path))
    try:
        if pkg.title_id != 0x454108DF or pkg.content_type != 2:
            raise ValueError('DLC must be an Xbox 360 Dead Space 2 content-type 00000002 package')
        entries = pkg.entries()
        paths = safe_paths(entries)
        dest = user / '0000000000000000' / '454108DF' / '00000002' / Path(path).name
        if dest.exists():
            for i, name in paths.items():
                e = entries[i]
                if e['dir']:
                    continue
                out = dest.joinpath(*PurePosixPath(name).parts)
                if not out.is_file() or out.stat().st_size != e['size']:
                    raise ValueError('Existing DLC differs; no files overwritten: ' + name)
                remaining = e['size']
                with out.open('rb') as f:
                    for block in pkg.blocks(e['start'], e['blocks'], e['contig']):
                        take = block[:remaining]
                        if f.read(len(take)) != take:
                            raise ValueError('Existing DLC differs; no files overwritten: ' + name)
                        remaining -= len(take)
                        if remaining == 0: break
                if remaining: raise ValueError('Incomplete DLC package: ' + name)
            log('DLC already installed; package bytes verified.')
            return {'display':pkg.display, 'files':sum(not entries[i]['dir'] for i in paths),
                    'path':str(dest),'already_installed':True}
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='ds2-dlc-', dir=dest.parent) as tmp:
            stage = Path(tmp) / 'content'
            stage.mkdir()
            count = 0
            for i, name in paths.items():
                e = entries[i]
                out = stage.joinpath(*PurePosixPath(name).parts)
                if e['dir']:
                    out.mkdir(parents=True, exist_ok=True)
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                remaining = e['size']
                with out.open('wb') as f:
                    for block in pkg.blocks(e['start'], e['blocks'], e['contig']):
                        take = block[:remaining]
                        f.write(take)
                        remaining -= len(take)
                        if remaining == 0:
                            break
                if remaining:
                    raise ValueError('Incomplete DLC file: ' + name)
                count += 1
            stage.rename(dest)
        log('Installed DLC: %s (%d files)' % (pkg.display, count))
        return {'display': pkg.display, 'files': count, 'path': str(dest)}
    finally:
        pkg.f.close()

def verify_runtime():
    manifest = json.loads((BASE / 'runtime-manifest.json').read_text())
    if manifest['target'] != 'tu2':
        raise ValueError('Package is not a TU2 runtime')
    if set(manifest.get('sha256', {})) != REQUIRED_RUNTIME:
        raise ValueError('Runtime manifest must contain all three required runtime files')
    for name, sha in manifest['sha256'].items():
        if Path(name).name != name:
            raise ValueError('Unsafe runtime manifest')
        if not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha):
            raise ValueError('Invalid runtime hash: ' + name)
        if digest((BASE / name).read_bytes()) != sha:
            raise ValueError('Runtime integrity check failed: ' + name)
    profile = manifest.get('profile_sha256', {})
    expected = settings_model.PROFILE_FILES
    if manifest.get('profile') != 'claude60-xenos' or set(profile) != expected:
        raise ValueError('Missing or incomplete 60 FPS profile')
    for name, sha in profile.items():
        if digest((BASE / name).read_bytes()) != sha:
            raise ValueError('Profile integrity check failed: ' + name)

def prepare(settings, log=print):
    settings_model.validate(settings)
    verify_runtime()
    user = Path(settings.get('user') or '.').resolve()
    disc1 = disc_image(settings['disc1'], user, log)
    log('Checking Disc 1 executable…')
    xex = read_xex(disc1)
    if digest(xex) != XEX_SHA:
        raise ValueError('Disc 1 does not match the supported World (En,Fr,Es) release')
    log('Checking TU2 patch…')
    patch = read_patch(settings['update'])
    active = int(settings.get('active_disc', 1))
    if active not in (1,2):
        raise ValueError('Launch disc must be 1 or 2')
    discs = {1:(disc1,xex,patch)}
    if settings.get('disc2'):
        disc2 = disc_image(settings['disc2'], user, log)
        log('Checking optional Disc 2…')
        second = read_xex(disc2)
        from disc2_identity import XEX_SHA256 as DISC2_SHA
        if digest(second) != DISC2_SHA:
            raise ValueError('Disc 2 does not match the verified World (En,Fr,Es) release')
        # A full TU package contains both patches. An extracted Disc 1 patch
        # may be paired with its sibling disc002/default.xexp from the same TU.
        update2 = Path(settings['update'])
        if update2.name.lower() == 'default.xexp':
            update2 = update2.parent.parent/'disc002/default.xexp'
        discs[2] = (disc2, second, read_patch(update2,2))
    if active not in discs:
        raise ValueError('Select an optional Disc 2 ISO before launching Disc 2')
    user.mkdir(parents=True, exist_ok=True)
    # Content addressed staging never overwrites an existing game or cache.
    staged = {}
    for number,(iso,ex,pat) in discs.items():
        game = user/'launcher-game'/digest(pat)
        game.parent.mkdir(parents=True,exist_ok=True)
        expected = {'default.xex':ex,'default.xexp':pat}
        if game.exists():
            for name,data in expected.items():
                target=game/name
                if not target.is_file() or target.read_bytes()!=data:
                    raise ValueError('Existing staged content differs: '+str(target))
        else:
            with tempfile.TemporaryDirectory(prefix='stage-',dir=game.parent) as tmp:
                folder=Path(tmp)/'game';folder.mkdir()
                for name,data in expected.items():(folder/name).write_bytes(data)
                folder.rename(game)
        staged[str(number)]={'iso':str(iso),'game':str(game),'xex_sha256':digest(ex),'xexp_sha256':digest(pat)}
    selected=staged[str(active)]
    receipt = {'target': 'tu2', 'disc1': str(disc1), 'user': str(user),
               'xex_sha256':selected['xex_sha256'],'xexp_sha256':selected['xexp_sha256'],
               'game':selected['game'],'iso':selected['iso'],'active_disc':active,'discs':staged}
    if settings.get('dlc'):
        log('Installing DLC…')
        receipt['dlc'] = install_dlc(settings['dlc'], user, log)
    profile, options = settings_model.create_profile(BASE, user, settings)
    receipt['mods_dir'] = str(profile)
    receipt['options'] = options
    atomic_json(user/'launcher-receipt.json',receipt)
    log(f'PASS: Disc {active} + TU2 ready. '+('Disc 2 add-on verified.' if 2 in discs else 'Disc 2 not added.'))
    return receipt

def command(settings, receipt):
    options = settings_model.validate(settings)
    # SDK src/system/runtime.cpp:31-34, src/ui/window.cpp:37,
    # src/input/mnk/mnk_input_driver.cpp:26; game src/app/ds2_app.h:43.
    # readback_resolve=none: research/sprint-S0-S1/gateC-readback-none (min 53.6 FPS) vs
    # fast resolve_wait 2.5-3.8 s per 10 s in the effects room (s2-settings-off, gateA).
    args = [str(BASE / 'ds2.exe'), '--ds2_iso=' + receipt.get('iso',receipt['disc1']),
            '--game_data_root=' + receipt['game'], '--user_data_root=' + receipt['user'],
            '--gpu_plugin=xenos', '--ds2_mods=true', '--ds2_mods_dir=' + receipt.get('mods_dir', str(BASE / 'mods')),
            '--clock_no_scaling=true', '--clock_source_raw=true', '--readback_resolve=none',
            '--log_level=info', '--fullscreen=' + str(settings['fullscreen']).lower(),
            '--mnk_mode=' + str(settings['keyboard']).lower(),
            '--user_language=' + str(settings_model.LANGUAGE_IDS[options['language']])]
    for number, disc in receipt.get('discs', {}).items():
        args.append('--ds2_disc' + number + '_iso=' + disc['iso'])
    return args + settings_model.window_arguments(settings)

def gui():
    from themed_ui import gui as show
    show(sys.modules[__name__])

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--disc1')
    parser.add_argument('--disc2', default='')
    parser.add_argument('--active-disc', type=int, choices=(1,2), default=1)
    parser.add_argument('--update')
    parser.add_argument('--user')
    parser.add_argument('--dlc', default='')
    args = parser.parse_args()
    if args.verify:
        try:
            prepare(vars(args))
        except (Exception, SystemExit) as e:
            print('FAIL:', e)
            sys.exit(1)
    else:
        gui()
