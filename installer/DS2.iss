#define AppVersion "2026.10.17"
[Setup]
AppId={{FBE62687-9B17-4F54-B334-EDECE61FAD2D}
AppName=Dead Space 2 Recomp TU2 Playtest
AppVersion={#AppVersion}
AppPublisher=Dead Space 2 Recomp contributors
AppPublisherURL=https://github.com/bifftannen1/Dead-Space-2-Recomp
DefaultDirName={localappdata}\Programs\Dead Space 2 Recomp
DefaultGroupName=Dead Space 2 Recomp
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=DS2-TU2-Playtest-20261017-Setup
SetupIconFile=..\launcher\rig.ico
UninstallDisplayIcon={app}\DS2Launcher.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
CloseApplications=yes
CloseApplicationsFilter=DS2Launcher.exe,ds2.exe
RestartApplications=no
[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
[Files]
Source: "..\build-installer\payload\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\Dead Space 2 Recomp"; Filename: "{app}\DS2Launcher.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\Dead Space 2 Recomp"; Filename: "{app}\DS2Launcher.exe"; WorkingDir: "{app}"; Tasks: desktopicon
[Run]
Filename: "{app}\DS2Launcher.exe"; Description: "Open Dead Space 2 Recomp launcher"; Flags: nowait postinstall skipifsilent unchecked
