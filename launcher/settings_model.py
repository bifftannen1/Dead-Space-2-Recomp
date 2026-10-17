"""Closed launcher settings contract; see tools/audit_launcher_settings.py."""
import hashlib
import json
from pathlib import Path
import tomllib

CHOICES = {
    'language': ('English', 'Français', 'Español'),
    'frame_limit': ('30 FPS', '60 FPS (experimental)'),
    'window_size': ('Automatic', '1280 x 720', '1600 x 900', '1920 x 1080', '2560 x 1440'),
    'fov': ('Original (1.00x)', 'Wider (1.10x)', 'Wide (1.25x)'),
}
DEFAULTS = {'language':'English', 'frame_limit':'60 FPS (experimental)', 'window_size':'Automatic', 'fov':'Original (1.00x)'}
# SDK include/rex/system/xcontent.h:23-29. DS2's UIFrontEndManager calls
# XTLGetLanguage (src_tu2/DeadSpace/src/modules/ui/uifrontendmanager.cpp:633),
# which reads XCONFIG_USER_LANGUAGE (XDK/core/private/xtl/xapi/getlang.c.cpp:46).
LANGUAGE_IDS = {'English': 1, 'Français': 4, 'Español': 5}
FOV = {'Original (1.00x)':1.0, 'Wider (1.10x)':1.1, 'Wide (1.25x)':1.25}
PROFILE_FILES = {'mods/framerate.toml','mods/framerate/framerate.cpp',
                 'mods/fov.toml','mods/fov/fov.cpp'}

def validate(settings):
    result={}
    for key, default in DEFAULTS.items():
        value=settings.get(key,default)
        if value not in CHOICES[key]:
            raise ValueError('Unsupported '+key+': '+repr(value))
        result[key]=value
    for key in ('fullscreen','keyboard'):
        value=settings.get(key,True)
        if type(value) is not bool: raise ValueError(key+' must be true or false')
        result[key]=value
    return result

def display_metrics():
    """(dpi scale, screen width, screen height) in physical pixels; (1, 0, 0) off Windows."""
    try:
        import ctypes
        user32=ctypes.windll.user32
        mode=_devmode()
        if not user32.EnumDisplaySettingsW(None,-1,ctypes.byref(mode)):  # ENUM_CURRENT_SETTINGS
            return 1.0, 0, 0
        # A DPI-unaware process sees scaled metrics and DPI 96; an aware one the reverse.
        scale=max(user32.GetDpiForSystem()/96, mode.dmPelsWidth/max(1,user32.GetSystemMetrics(0)))
        return scale, mode.dmPelsWidth, mode.dmPelsHeight
    except (AttributeError, OSError):
        return 1.0, 0, 0

def _devmode():
    import ctypes
    from ctypes import wintypes
    class DEVMODEW(ctypes.Structure):
        _fields_=[('dmDeviceName',wintypes.WCHAR*32),('dmSpecVersion',wintypes.WORD),
                  ('dmDriverVersion',wintypes.WORD),('dmSize',wintypes.WORD),
                  ('dmDriverExtra',wintypes.WORD),('dmFields',wintypes.DWORD),
                  ('dmPosition',wintypes.LONG*2),('dmDisplayOrientation',wintypes.DWORD),
                  ('dmDisplayFixedOutput',wintypes.DWORD),('dmColor',ctypes.c_short),
                  ('dmDuplex',ctypes.c_short),('dmYResolution',ctypes.c_short),
                  ('dmTTOption',ctypes.c_short),('dmCollate',ctypes.c_short),
                  ('dmFormName',wintypes.WCHAR*32),('dmLogPixels',wintypes.WORD),
                  ('dmBitsPerPel',wintypes.DWORD),('dmPelsWidth',wintypes.DWORD),
                  ('dmPelsHeight',wintypes.DWORD),('dmDisplayFlags',wintypes.DWORD),
                  ('dmDisplayFrequency',wintypes.DWORD)]
    mode=DEVMODEW();mode.dmSize=ctypes.sizeof(DEVMODEW)
    return mode

def window_arguments(settings, metrics=None):
    # The runtime sizes the window in logical pixels (src/ui/window.cpp:24) and Windows
    # multiplies by display scaling, so 1920x1080 at 150% became a 2880x1620 window.
    # Options are physical pixels: divide by the scale and keep the window on screen.
    options=validate(settings)
    size=options['window_size']
    if size=='Automatic':
        return ['--window_width=0','--window_height=0']
    width,height=map(int,size.split(' x '))
    scale,screen_w,screen_h=metrics or display_metrics()
    if screen_w and screen_h:
        fit=min(1.0,screen_w/width,(screen_h*0.92)/height)
        width,height=int(width*fit),int(height*fit)
    return ['--window_width='+str(round(width/scale)),'--window_height='+str(round(height/scale))]

def create_profile(base, user, settings):
    """Create an immutable per-settings manifest folder, leaving packaged mods intact."""
    options=validate(settings)
    manifests={}
    for name in ('framerate','fov'):
        text=(Path(base)/'mods'/ (name+'.toml')).read_text()
        before=tomllib.loads(text)
        if before['mod']['name']!=name: raise ValueError('Wrong packaged mod identity')
        # Only the settings table and explicit enabled bit are replaced; hook names
        # and source identities remain the audited, compiled manifest's values.
        head,sep,_=text.partition('[settings]')
        if not sep or head.count('enabled = ')!=1: raise ValueError('Unexpected mod manifest')
        import re
        enabled = name=='framerate' or FOV[options['fov']]!=1.0
        head=re.sub(r'^enabled = (?:true|false)$','enabled = '+str(enabled).lower(),head,flags=re.M)
        tail=('target_vblanks = '+('2' if options['frame_limit']=='30 FPS' else '1')+'\nlog_fps = 1\n'
              if name=='framerate' else 'fov_scale = '+str(FOV[options['fov']])+'\nlog_calls = 5\n')
        manifests[name+'.toml']=(head+'[settings]\n'+tail).encode()
    identity=hashlib.sha256(b''.join(k.encode()+v for k,v in sorted(manifests.items()))).hexdigest()
    folder=Path(user)/'launcher-profiles'/identity
    folder.mkdir(parents=True,exist_ok=True)
    for name,data in manifests.items():
        dest=folder/name
        if dest.exists():
            if dest.read_bytes()!=data: raise ValueError('Existing launch profile differs: '+str(dest))
        else:
            with dest.open('xb') as f: f.write(data)
    return folder,options
