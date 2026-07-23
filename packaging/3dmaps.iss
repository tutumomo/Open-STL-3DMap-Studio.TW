; Inno Setup script voor Open STL-3DMap Studio
; Compileren:  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" packaging\3dmaps.iss
; Vereist dat de PyInstaller-build al in "dist\Open STL-3DMap Studio" staat.

#define AppName "Open STL-3DMap Studio"
#define AppVersion "1.1.1"
#define AppPublisher "OpenAEC Foundation"
#define AppExe "Open STL-3DMap Studio.exe"
#define SourceDir "..\dist\Open STL-3DMap Studio"
#define RepoUrl "https://github.com/OpenAEC-Foundation/Open-STL-3DMap-Studio"

[Setup]
; Deze GUID identificeert het programma bij updates en verwijderen. Niet wijzigen.
AppId={{C1E7A2F4-9B3D-4E6A-8F21-7A0C5D9E4B12}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}/issues
AppUpdatesURL={#RepoUrl}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputDir=Output
OutputBaseFilename=OpenSTL-3DMapStudio-{#AppVersion}-Setup
SetupIconFile=app.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Standaard installeren voor de huidige gebruiker: geen UAC-prompt nodig. Wie
; het voor alle gebruikers wil, kan dat in de installer alsnog kiezen.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DisableProgramGroupPage=yes
DisableDirPage=no

[Languages]
Name: "dutch"; MessagesFile: "compiler:Languages\Dutch.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; DestName: "Handleiding.md"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Handleiding"; Filename: "{app}\Handleiding.md"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Gedownloade kaartdata en logbestand. Projecten en gegenereerde STL's blijven
; staan - die horen de gebruiker toe, niet de installatie.
Type: filesandordirs; Name: "{localappdata}\{#AppName}\cache"
Type: files; Name: "{localappdata}\{#AppName}\log.txt"
