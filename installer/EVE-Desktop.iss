; EVE Desktop 安装包脚本（Inno Setup 6.3+）
; 构建：把 dist-app\EVE-Desktop\ 整个目录打进安装包，
; 可选组件「Wireshark」在安装时从官方镜像在线下载并静默安装。
;
;   ISCC.exe EVE-Desktop.iss

#define MyAppName "EVE Desktop"
#define MyAppVersion "0.2.0"
#define MyAppExeName "EVE-Desktop.exe"

; Wireshark 官方「latest」直链（三个镜像轮换，Wireshark-latest-x64.exe 永远指向当前稳定版）
#define WiresharkUrl1 "https://2.na.dl.wireshark.org/win64/Wireshark-latest-x64.exe"
#define WiresharkUrl2 "https://1.eu.dl.wireshark.org/win64/Wireshark-latest-x64.exe"

[Setup]
AppId={{8E2B6A21-5B1D-4C0A-9F3E-1A7D52C90E11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
DefaultDirName={autopf}\EVE-Desktop
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=Output
OutputBaseFilename=EVE-Desktop-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=6.3

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; \
    Description: "创建桌面快捷方式(&D)"; \
    GroupDescription: "附加任务："
Name: "wireshark"; \
    Description: "安装 Wireshark（抓包用，约 93MB，安装时需联网下载；已装则自动跳过）(&W)"; \
    GroupDescription: "附加组件："; \
    Flags: unchecked

[Files]
Source: "..\dist\EVE-Desktop\*"; \
    DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; \
    Description: "启动 {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

[Code]
// ---------- Wireshark 检测：装过就不再下 ----------
function WiresharkInstalled(): Boolean;
var
  p: string;
begin
  Result := False;
  // 先看注册表（官方安装器会写）
  if RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Wireshark',
       'InstallLocation', p) and (p <> '') and FileExists(AddBackslash(p) + 'Wireshark.exe') then begin
    Result := True;
    exit;
  end;
  // 再看常见安装位置（capture.py 的 wireshark_exe() 找的就是这几处）
  if FileExists('C:\Program Files\Wireshark\Wireshark.exe') or
     FileExists('C:\Program Files (x86)\Wireshark\Wireshark.exe') or
     FileExists(AddBackslash(ExpandConstant('{userappdata}')) + 'Programs\Wireshark\Wireshark.exe') then
    Result := True;
end;

// 依次尝试多个镜像，全部失败返回 False（不拦安装）
function DownloadWireshark(var Path: string): Boolean;
begin
  Result := False;
  WizardForm.StatusLabel.Caption := '正在下载 Wireshark（约 93MB，视网速需要几分钟）…';
  try
    Path := ExpandConstant('{tmp}\Wireshark-setup.exe');
    try
      DownloadTemporaryFile('{#WiresharkUrl1}', Path, '', nil);
      Result := True;
    except
      // 镜像 1 失败 → 试镜像 2
      try
        DownloadTemporaryFile('{#WiresharkUrl2}', Path, '', nil);
        Result := True;
      except
        Result := False;
      end;
    end;
  finally
    WizardForm.StatusLabel.Caption := '';
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Path: string;
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then exit;
  if not WizardIsTaskSelected('wireshark') then exit;
  if WiresharkInstalled() then begin
    Log('Wireshark 已安装，跳过');
    exit;
  end;

  if not DownloadWireshark(Path) then begin
    // 不联网/镜像全挂：不打断安装，提示抓包功能需手动装 Wireshark
    MsgBox('Wireshark 下载失败（可能没有联网）。' #13#10
           'EVE Desktop 已安装完成；以后需要抓包时，请到 https://www.wireshark.org 手动安装 Wireshark。',
           mbInformation, MB_OK);
    exit;
  end;

  WizardForm.StatusLabel.Caption := '正在安装 Wireshark…';
  try
    // Wireshark 官方安装器是 NSIS 包：/S 静默
    if Exec(Path, '/S', '', SW_SHOW, ewWaitUntilTerminated, ResultCode) then begin
      if not WiresharkInstalled() then
        Log('Wireshark 安装器退出码 ' + IntToStr(ResultCode) + '，但未检测到安装');
    end else
      MsgBox('Wireshark 安装程序启动失败，可以以后到 https://www.wireshark.org 手动安装。',
             mbInformation, MB_OK);
  finally
    WizardForm.StatusLabel.Caption := '';
  end;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
end;
