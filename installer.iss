#define AppName "Музыкальная полка"
#define AppVersion "1.0.0"
#define AppExeName "MusicShelf.exe"

[Setup]
AppId={{B9AD5848-4E2B-4B2B-BF1D-0CA3D1E48F50}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Музыкальная полка
DefaultDirName={autopf}\Music Shelf
DefaultGroupName={#AppName}
LicenseFile=LICENSE
SetupIconFile=assets\music-shelf.ico
DisableProgramGroupPage=yes
OutputDir=release
OutputBaseFilename=music-shelf-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "dist\MusicShelf\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Открыть {#AppName}"; Flags: nowait postinstall skipifsilent
