; =====================================================================
;  SC_Toolbox — Inno Setup Installer Script
;
;  Run build_installer.bat first to populate the staging/ directory,
;  then this script is invoked automatically by the build process.
;
;  To compile manually:  iscc SC_Toolbox_Installer.iss
; =====================================================================

#define MyAppName      "SC Toolbox"
#define MyAppVersion   "2.2.6"
#define MyAppPublisher "SC Toolbox"
#define MyAppURL       "https://github.com/ScPlaceholder/SC-Toolbox-Beta-V2"

[Setup]
AppId={{8F3E2A7B-4C1D-4E5F-9A8B-6D2C7E1F0A3B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=SC_Toolbox_Setup_{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\assets\sc_toolbox.ico
UninstallDisplayIcon={app}\sc_toolbox.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Bundled Python interpreter + site-packages
Source: "staging\python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs

; Application source code
Source: "staging\skill_launcher.py";            DestDir: "{app}"; Flags: ignoreversion
Source: "staging\skill_launcher_settings.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "staging\pyproject.toml";               DestDir: "{app}"; Flags: ignoreversion
Source: "staging\README.txt";                   DestDir: "{app}"; Flags: ignoreversion isreadme

; Launcher wrapper (hides console window, checks bundled Python first)
Source: "staging\SC_Toolbox.vbs"; DestDir: "{app}"; Flags: ignoreversion

; App icon
Source: "staging\sc_toolbox.ico"; DestDir: "{app}"; Flags: ignoreversion

; Core modules
Source: "staging\core\*";   DestDir: "{app}\core";   Flags: ignoreversion recursesubdirs createallsubdirs
Source: "staging\shared\*"; DestDir: "{app}\shared"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "staging\ui\*";     DestDir: "{app}\ui";     Flags: ignoreversion recursesubdirs createallsubdirs

; Skills
Source: "staging\skills\*"; DestDir: "{app}\skills"; Flags: ignoreversion recursesubdirs createallsubdirs

; Tools (Battle Buddy, Mining Signals, etc.) — exclude py313_paddleocr (deployed separately below)
Source: "staging\tools\*"; DestDir: "{app}\tools"; Excludes: "py313_paddleocr"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist

; Mining Signals — PaddleOCR embedded Python (deployed to user-local path expected by paddle_client.py)
Source: "staging\tools\Mining_Signals\py313_paddleocr\*"; DestDir: "{localappdata}\SC_Toolbox\py313_paddleocr"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist

; Mining Signals — PaddleOCR neural network models (pre-bundled so first run is fully offline)
Source: "staging\paddlex_models\*"; DestDir: "{%USERPROFILE}\.paddlex\official_models"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist

; Locales (if present)
Source: "staging\locales\*"; DestDir: "{app}\locales"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist

[Icons]
; Start Menu
Name: "{group}\{#MyAppName}"; Filename: "{app}\SC_Toolbox.vbs"; WorkingDir: "{app}"; IconFilename: "{app}\sc_toolbox.ico"; Comment: "Launch SC Toolbox"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"

; Desktop shortcut
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\SC_Toolbox.vbs"; WorkingDir: "{app}"; IconFilename: "{app}\sc_toolbox.ico"; Tasks: desktopicon; Comment: "Launch SC Toolbox"

[Run]
Filename: "{app}\SC_Toolbox.vbs"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent shellexec

[UninstallDelete]
; Clean up runtime-generated files on uninstall
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\__pycache__"
