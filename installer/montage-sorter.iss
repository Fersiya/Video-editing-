; Сборка: iscc installer\montage-sorter.iss /DAppVersion=1.0.0
; Перед этим нужна папка build\ (embedded Python + app + web + bin\ffmpeg), её готовит GitHub Actions.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6B1B7C0E-4C55-4C3E-9A55-5D6E0C7A1F21}
AppName=Montage Sorter
AppVersion={#AppVersion}
AppPublisher=Fersiya
DefaultDirName={localappdata}\Programs\Montage Sorter
DefaultGroupName=Montage Sorter
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\Output
OutputBaseFilename=MontageSorter-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Ярлыки:"
Name: "ai_cpu"; Description: "ИИ-режим на процессоре (докачает ~1.5 ГБ: движок ~300 МБ и модели)"; GroupDescription: "Сортировка по содержимому (ИИ):"; Flags: exclusive
Name: "ai_gpu"; Description: "ИИ-режим на видеокарте NVIDIA (докачает ~3.5 ГБ, только для карт NVIDIA)"; GroupDescription: "Сортировка по содержимому (ИИ):"; Flags: exclusive unchecked
Name: "ai_none"; Description: "Без ИИ (сортировка только по типам файлов)"; GroupDescription: "Сортировка по содержимому (ИИ):"; Flags: exclusive unchecked

[Files]
Source: "..\build\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Montage Sorter"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m app --autoexit"; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"
Name: "{autodesktop}\Montage Sorter"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m app --autoexit"; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
; ИИ-модули ставятся во встроенный Python (нужен интернет)
Filename: "{app}\python\python.exe"; Parameters: "-m pip install --no-warn-script-location transformers torch"; StatusMsg: "Ставлю ИИ-модули (несколько минут, идёт в чёрном окне)…"; Tasks: ai_cpu
Filename: "{app}\python\python.exe"; Parameters: "-m pip install --no-warn-script-location torch --index-url https://download.pytorch.org/whl/cu124"; StatusMsg: "Ставлю PyTorch для видеокарты (несколько минут, идёт в чёрном окне)…"; Tasks: ai_gpu
Filename: "{app}\python\python.exe"; Parameters: "-m pip install --no-warn-script-location transformers"; StatusMsg: "Ставлю transformers…"; Tasks: ai_gpu
Filename: "{app}\python\python.exe"; Parameters: "-m app.prefetch"; WorkingDir: "{app}"; StatusMsg: "Скачиваю модели ИИ (около 1.2 ГБ)…"; Tasks: ai_cpu ai_gpu
Filename: "{app}\python\pythonw.exe"; Parameters: "-m app --autoexit"; WorkingDir: "{app}"; Description: "Запустить Montage Sorter"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app"
