"""Build the installer from the exact published portable package (Inno Setup 6)."""
import hashlib, json, subprocess, sys, zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
ARCHIVE=ROOT/'dist/DS2-TU2-Playtest-20261017.zip'
EXPECTED='687acb7f82c0ea6f68cc7a6105819c0e127b4a31616e3434c50a98f800f84295'
def main():
    assert hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()==EXPECTED,'Unexpected portable package'
    payload=ROOT/'build-installer/payload';payload.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(ARCHIVE) as z:
        for name in z.namelist():
            rel=Path(name).relative_to('DS2-TU2-Alpha')
            dest=(payload/rel).resolve();assert payload.resolve() in dest.parents
            if name.endswith('/'):continue
            dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(z.read(name))
    compiler=Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'build-installer/inno/ISCC.exe'
    subprocess.run([str(compiler),str(ROOT/'installer/DS2.iss')],check=True)
if __name__=='__main__':main()
