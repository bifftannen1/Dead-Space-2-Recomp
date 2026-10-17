"""Exercise real setup/reinstall/uninstall in a dedicated directory; retain user files."""
import hashlib,json,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
TEST=ROOT/'build-installer/test-install'
SETUP=ROOT/'dist/DS2-TU2-Playtest-20261017-Setup.exe'
REPORT=ROOT/'build-installer/test-results.json'
def run(exe,*args):
    result=subprocess.run([str(exe),*args],check=True);assert result.returncode==0

def verify():
    with zipfile.ZipFile(ROOT/'dist/DS2-TU2-Playtest-20261017.zip') as z:
        count=0
        for name in z.namelist():
            if name.endswith('/'):continue
            dest=TEST/Path(name).relative_to('DS2-TU2-Alpha')
            assert dest.read_bytes()==z.read(name),name
            count+=1
    return count

def main():
    assert TEST.resolve().parent== (ROOT/'build-installer').resolve()
    if sys.argv[1]=='install':
        assert not TEST.exists(),'Use a fresh test directory'
        run(SETUP,'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-',f'/DIR={TEST}','/GROUP=DS2 Installer Verification','/TASKS=','/LOG='+str(ROOT/'build-installer/install.log'))
        count=verify()
        (TEST/'user-save-test.txt').write_text('preserve user content')
        run(SETUP,'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-',f'/DIR={TEST}','/GROUP=DS2 Installer Verification','/TASKS=','/LOG='+str(ROOT/'build-installer/reinstall.log'))
        assert verify()==count
        assert (TEST/'user-save-test.txt').read_text()=='preserve user content'
        REPORT.write_text(json.dumps({'installed_files_verified':count,'reinstall_passed':True,'user_file_preserved_on_reinstall':True},indent=2))
    elif sys.argv[1]=='uninstall':
        run(TEST/'unins000.exe','/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/LOG='+str(ROOT/'build-installer/uninstall.log'))
        assert not (TEST/'DS2Launcher.exe').exists()
        assert not (TEST/'ds2.exe').exists()
        assert (TEST/'user-save-test.txt').read_text()=='preserve user content'
        d=json.loads(REPORT.read_text());d.update(uninstall_passed=True,user_file_preserved_on_uninstall=True,installer_sha256=hashlib.sha256(SETUP.read_bytes()).hexdigest());REPORT.write_text(json.dumps(d,indent=2));print(json.dumps(d))
if __name__=='__main__':main()
