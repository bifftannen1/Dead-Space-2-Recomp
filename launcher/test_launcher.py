import unittest
import tempfile
import json
import zipfile
from pathlib import Path
from unittest.mock import patch
import launcher

class Checks(unittest.TestCase):
    def setUp(self):
        tempfile.tempdir = str(Path(__file__).resolve().parents[1]/'build-temp')
        self.profile_base=patch.object(launcher,'BASE',Path(__file__).resolve().parents[1]/'profiles/claude60')
        self.profile_base.start();self.addCleanup(self.profile_base.stop)
    def test_parent_cycle(self):
        with self.assertRaises(ValueError):
            launcher.safe_paths([dict(name='a', parent=0, dir=True)])

    def test_traversal(self):
        for name in ['..', '../escape', 'C:escape', 'a\\b']:
            with self.assertRaises(ValueError):
                launcher.safe_paths([dict(name=name, parent=-1, dir=False)])

    def test_case_collision(self):
        with self.assertRaises(ValueError):
            launcher.safe_paths([dict(name=n, parent=-1, dir=False) for n in ['A', 'a']])

    def test_missing_parent(self):
        with self.assertRaises(ValueError):
            launcher.safe_paths([dict(name='a', parent=8, dir=False)])

    def test_windows_reserved_paths(self):
        for name in ['CON','nul.txt','COM1','LPT9.txt','file.','file ','file?','a\x00b']:
            with self.assertRaises(ValueError):
                launcher.safe_paths([dict(name=name,parent=-1,dir=False)])

    def test_empty_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'runtime-manifest.json').write_text(json.dumps({'target':'tu2','sha256':{}}))
            with patch.object(launcher,'BASE',p), self.assertRaises(ValueError):
                launcher.verify_runtime()

    def test_runtime_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            for name in launcher.REQUIRED_RUNTIME:(p/name).write_bytes(b'original')
            data={'target':'tu2','sha256':{n:launcher.digest(b'original') for n in launcher.REQUIRED_RUNTIME}}
            data['profile']='claude60-xenos'
            data['profile_sha256']={}
            for name in launcher.settings_model.PROFILE_FILES:
                (p/name).parent.mkdir(parents=True,exist_ok=True)
                (p/name).write_bytes(b'profile')
                data['profile_sha256'][name]=launcher.digest(b'profile')
            (p/'runtime-manifest.json').write_text(json.dumps(data))
            with patch.object(launcher,'BASE',p):
                launcher.verify_runtime()
                (p/'ds2.exe').write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError,'integrity'):launcher.verify_runtime()
                (p/'ds2.exe').write_bytes(b'original')
                (p/'mods/framerate.toml').write_bytes(b'disabled')
                with self.assertRaisesRegex(ValueError,'Profile integrity'):launcher.verify_runtime()

    def test_truncated_stfs_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'bad';p.write_bytes(b'LIVE')
            with self.assertRaisesRegex(ValueError,'Truncated'):launcher.Package(p)

    def test_optional_disc2_not_required(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(launcher,'verify_runtime'), patch.object(launcher,'read_xex',return_value=b'one'), patch.object(launcher,'read_patch',return_value=b'patch'), patch.object(launcher,'XEX_SHA',launcher.digest(b'one')):
            receipt=launcher.prepare(dict(disc1='one.iso',update='tu',user=tmp),lambda _:None)
            self.assertEqual(receipt['active_disc'],1)
            self.assertEqual(set(receipt['discs']),{'1'})

    def test_disc2_selection_requires_addon(self):
        with patch.object(launcher,'verify_runtime'), patch.object(launcher,'read_xex',return_value=b'one'), patch.object(launcher,'read_patch',return_value=b'patch'), patch.object(launcher,'XEX_SHA',launcher.digest(b'one')):
            with self.assertRaisesRegex(ValueError,'optional Disc 2'):
                launcher.prepare(dict(disc1='one.iso',update='tu',active_disc=2),lambda _:None)

    def test_disc2_uses_its_own_patch_and_iso(self):
        import disc2_identity
        with tempfile.TemporaryDirectory() as tmp, patch.object(launcher,'verify_runtime'), patch.object(launcher,'read_xex',side_effect=[b'one',b'two']), patch.object(launcher,'read_patch',side_effect=[b'p1',b'p2']), patch.object(launcher,'XEX_SHA',launcher.digest(b'one')), patch.object(disc2_identity,'XEX_SHA256',launcher.digest(b'two')):
            s=dict(disc1='one.iso',disc2='two.iso',update='tu.zip',active_disc=2,user=tmp,fullscreen=False,keyboard=True)
            r=launcher.prepare(s,lambda _:None)
            self.assertEqual(Path(r['game'],'default.xex').read_bytes(),b'two')
            self.assertEqual(Path(r['game'],'default.xexp').read_bytes(),b'p2')
            self.assertEqual(r['iso'],str(Path('two.iso').resolve()))
            self.assertIn('--ds2_iso='+r['iso'],launcher.command(s,r))
            self.assertIn('--ds2_disc1_iso='+r['discs']['1']['iso'],launcher.command(s,r))
            self.assertIn('--ds2_disc2_iso='+r['discs']['2']['iso'],launcher.command(s,r))

    def test_existing_stage_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(launcher,'verify_runtime'), patch.object(launcher,'read_xex',return_value=b'one'), patch.object(launcher,'read_patch',return_value=b'patch'), patch.object(launcher,'XEX_SHA',launcher.digest(b'one')):
            s=dict(disc1='one.iso',update='tu',user=tmp)
            r=launcher.prepare(s,lambda _:None);p=Path(r['game'])/'default.xex';p.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'differs'):launcher.prepare(s,lambda _:None)
            self.assertEqual(p.read_bytes(),b'changed')

    def test_wrong_disc_rejected_before_writes(self):
        with patch.object(launcher, 'verify_runtime'), patch.object(launcher, 'read_xex', return_value=b'XEX2bad'):
            with self.assertRaisesRegex(ValueError, 'Disc 1 does not match'):
                launcher.prepare({'disc1': 'unused'}, lambda _: None)

    def test_command_preserves_spaces_and_uses_supported_flags(self):
        s = dict(user='C:/my saves', fullscreen=False, keyboard=True)
        args = launcher.command(s, dict(disc1='C:/my discs/disc 1.iso', game='C:/my game', user='C:/my saves'))
        self.assertIn('--ds2_iso=C:/my discs/disc 1.iso', args)
        self.assertIn('--fullscreen=false', args)
        self.assertIn('--ds2_mods=true', args)
        self.assertIn('--clock_no_scaling=true', args)
        self.assertIn('--clock_source_raw=true', args)
        self.assertIn('--readback_resolve=none', args)
        self.assertIn('--gpu_plugin=xenos', args)
        self.assertNotIn('--ds2_iso_disc2', ' '.join(args))

    def test_zipped_iso_unpacked_once_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);z=p/'Disc 1.zip'
            with zipfile.ZipFile(z,'w') as f:f.writestr('Disc 1/game.iso',b'iso-bytes');f.writestr('readme.txt','x')
            logs=[];first=launcher.disc_image(z,p/'user',logs.append)
            self.assertEqual(first.read_bytes(),b'iso-bytes');self.assertEqual(first.name,'game.iso')
            again=launcher.disc_image(z,p/'user',logs.append)
            self.assertEqual(first,again);self.assertEqual(len(logs),1)
            plain=p/'plain.iso';plain.write_bytes(b'raw')
            self.assertEqual(launcher.disc_image(plain,p/'user',logs.append),plain.resolve())

    def test_zip_with_two_images_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            z=Path(tmp)/'both.zip'
            with zipfile.ZipFile(z,'w') as f:f.writestr('a.iso',b'1');f.writestr('b.iso',b'2')
            with self.assertRaisesRegex(ValueError,'exactly one'):launcher.disc_image(z,Path(tmp),print)

    def test_zipped_dlc_installs_inner_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);z=p/'Severed.zip'
            with zipfile.ZipFile(z,'w') as f:
                f.writestr('454108DF/00000002/1B7ECDF8',b'LIVE'+b'\0'*60);f.writestr('notes.txt','not a package')
            seen={}
            def fake(path,user,log):seen['name']=Path(path).name;seen['data']=Path(path).read_bytes()[:4];return {}
            with patch.object(launcher,'install_package',side_effect=fake):
                launcher.install_dlc(z,p,print)
            self.assertEqual(seen,{'name':'1B7ECDF8','data':b'LIVE'})
            self.assertEqual([x.name for x in p.iterdir()],['Severed.zip'])

    def test_window_size_is_physical_and_fits_screen(self):
        w=launcher.settings_model.window_arguments
        # 150% scaling on 1920x1200 (the 2880x1620 cut-off window in logs/ds2_012.log).
        self.assertEqual(w({'window_size':'1920 x 1080'},(1.5,1920,1200)),['--window_width=1280','--window_height=720'])
        self.assertEqual(w({'window_size':'1280 x 720'},(1.0,2560,1440)),['--window_width=1280','--window_height=720'])
        big=w({'window_size':'2560 x 1440'},(1.0,1920,1080))
        self.assertLessEqual(int(big[0].split('=')[1]),1920);self.assertLessEqual(int(big[1].split('=')[1]),1080)
        self.assertEqual(w({'window_size':'Automatic'},(1.5,1920,1200)),['--window_width=0','--window_height=0'])

if __name__ == '__main__':
    unittest.main()
