"""Freeze the launcher with its RIG icon; requires PyInstaller in the build environment."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

root=Path(__file__).resolve().parents[1]
icon=root/'launcher/rig.ico'
inputs=['launcher/launcher.py','launcher/themed_ui.py','launcher/settings_model.py','launcher/disc2_identity.py',
        'launcher/rig.ico','launcher/header.png','tools/extract_xex.py','tools/extract_dlc.py']
before={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in inputs}
subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--onefile','--windowed',
                '--name','DS2Launcher','--icon',str(icon),'--add-data',str(icon)+';.','--add-data',str(root/'launcher/header.png')+';.',
                '--paths',str(root/'tools'),'--distpath',str(root/'dist/DS2-TU2-Alpha'),
                '--workpath',str(root/'build-launcher'),'--specpath',str(root/'launcher'),
                str(root/'launcher/launcher.py')],check=True)
assert before=={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in inputs}
(root/'build-launcher/freeze-receipt.json').write_text(json.dumps({'sources':before,
    'exe_sha256':hashlib.sha256((root/'dist/DS2-TU2-Alpha/DS2Launcher.exe').read_bytes()).hexdigest()},indent=2))
