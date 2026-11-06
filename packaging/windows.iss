#define AppName "Kipofox Studio"
#define AppVersion "0.1.0"
[Setup]
AppId={{B2191648-0F11-4E0A-9406-E89C5F031580}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\Kipofox Studio
DefaultGroupName=Kipofox Studio
OutputDir=..\installer
OutputBaseFilename=KipofoxStudio-Setup-Windows
Compression=lzma
SolidCompression=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
[Files]
Source: "..\dist\KipofoxStudio.exe"; DestDir: "{app}"; Flags: ignoreversion
[Icons]
Name: "{group}\Kipofox Studio"; Filename: "{app}\KipofoxStudio.exe"
Name: "{autodesktop}\Kipofox Studio"; Filename: "{app}\KipofoxStudio.exe"
[Run]
Filename: "{app}\KipofoxStudio.exe"; Description: "Запустить Kipofox Studio"; Flags: nowait postinstall skipifsilent
