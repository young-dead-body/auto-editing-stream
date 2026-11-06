#define AppName "Stream Editor"
#define AppVersion "0.1.0"
[Setup]
AppId={{B2191648-0F11-4E0A-9406-E89C5F031580}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\Stream Editor
DefaultGroupName=Stream Editor
OutputDir=..\installer
OutputBaseFilename=StreamEditor-Setup-Windows
Compression=lzma
SolidCompression=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
[Files]
Source: "..\dist\StreamEditor.exe"; DestDir: "{app}"; Flags: ignoreversion
[Icons]
Name: "{group}\Stream Editor"; Filename: "{app}\StreamEditor.exe"
Name: "{autodesktop}\Stream Editor"; Filename: "{app}\StreamEditor.exe"
[Run]
Filename: "{app}\StreamEditor.exe"; Description: "Запустить Stream Editor"; Flags: nowait postinstall skipifsilent
