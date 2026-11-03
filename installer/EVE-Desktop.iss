; EVE Desktop 安装包脚本（Inno Setup 6.3+）
; 构建：把 dist\EVE-Desktop\ 整个目录打进安装包。
;
;   ISCC.exe EVE-Desktop.iss
;
; 关于 Wireshark（抓包用）：**不在这里在线下载**。
; 早期版本勾了「安装 Wireshark」就从 dl.wireshark.org 拉 90MB 的安装器，国内基本
; 连不上，于是装到一半弹「Wireshark 下载失败（可能没有联网）」—— 装是装上了，
; 但那个框看着像安装失败。现在改成两条路，都不需要联网：
;   a) 把官方安装器重命名成 installer\vendor\Wireshark-x64.exe，构建时自动随包，
;      安装时离线静默装（有这个文件才会出现「安装 Wireshark」这个选项）
;   b) 没随包时，安装完成页多一条「打开 Wireshark 下载页」，勾了才开浏览器；
;      本机已装则这条不显示。应用里抓包面板也有同一个入口。

#define MyAppName "EVE Desktop"
#define MyAppVersion "0.3.2"
#define MyAppExeName "EVE-Desktop.exe"

; 离线 Wireshark：放进来就随包（体积 +~90MB），不放就没有这个选项。
; 两种包输出到不同文件名（带 -full），免得同版本号互相覆盖、事后分不清哪个能离线装。
#define WiresharkBundle "vendor\Wireshark-x64.exe"
#define WiresharkName "Wireshark-x64.exe"
#if FileExists(SourcePath + WiresharkBundle)
  #define HAVE_WS
  #define SetupFileName "EVE-Desktop-Setup-" + MyAppVersion + "-full"
#else
  #define SetupFileName "EVE-Desktop-Setup-" + MyAppVersion
#endif

[Setup]
AppId={{8E2B6A21-5B1D-4C0A-9F3E-1A7D52C90E11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
DefaultDirName={autopf}\EVE-Desktop
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=Output
OutputBaseFilename={#SetupFileName}
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
#ifdef HAVE_WS
Name: "wireshark"; \
    Description: "安装 Wireshark（抓包用，随包离线安装，不联网；已装则跳过）(&W)"; \
    GroupDescription: "附加组件："
#endif

[Files]
Source: "..\dist\EVE-Desktop\*"; \
    DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
#ifdef HAVE_WS
Source: "{#WiresharkBundle}"; \
    DestDir: "{tmp}"; Flags: dontcopy
#endif

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; \
    Description: "启动 {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent
; 没随包 Wireshark 时的软提示：只是打开官网页面，下载与否由用户在浏览器里决定
Filename: "https://www.wireshark.org/download.html"; \
    Description: "打开 Wireshark 下载页（抓包要用；本机已装则不会出现）"; \
    Flags: postinstall shellexec nowait unchecked; \
    Check: not WiresharkInstalled

[Code]
// ---------- 本机有没有 Wireshark ----------
// 和 core/capture.py 的 wireshark_exe() 保持同一套判断，别一边认一边不认
function WiresharkInstalled(): Boolean;
var
  p: string;
begin
  Result := False;
  if RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Wireshark',
       'InstallLocation', p) and (p <> '') and FileExists(AddBackslash(p) + 'Wireshark.exe') then begin
    Result := True;
    exit;
  end;
  // 64 位安装器写在 64 位视图里
  if IsWin64 and RegQueryStringValue(HKLM64, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Wireshark',
       'InstallLocation', p) and (p <> '') and FileExists(AddBackslash(p) + 'Wireshark.exe') then begin
    Result := True;
    exit;
  end;
  // 注册表没有就看常见安装位置
  if FileExists('C:\Program Files\Wireshark\Wireshark.exe') or
     FileExists('C:\Program Files (x86)\Wireshark\Wireshark.exe') or
     FileExists(AddBackslash(ExpandConstant('{localappdata}')) + 'Programs\Wireshark\Wireshark.exe') then
    Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
#ifdef HAVE_WS
var
  ExePath: string;
  ResultCode: Integer;
#endif
begin
  if CurStep <> ssPostInstall then exit;
#ifdef HAVE_WS
  if not WizardIsTaskSelected('wireshark') then exit;
  if WiresharkInstalled() then begin
    Log('Wireshark 已安装，跳过');
    exit;
  end;

  WizardForm.StatusLabel.Caption := '正在安装 Wireshark…';
  try
    ExtractTemporaryFile('{#WiresharkName}');
    ExePath := ExpandConstant('{tmp}\{#WiresharkName}');
    // Wireshark 官方安装器是 NSIS 包：/S 静默
    if not Exec(ExePath, '/S', '', SW_SHOW, ewWaitUntilTerminated, ResultCode) then
      MsgBox('Wireshark 安装程序没能启动。' #13#10
             'EVE Desktop 已经装好了，抓包可以把包存到本机，以后手动装 Wireshark 也能用。',
             mbInformation, MB_OK)
    else if not WiresharkInstalled() then
      Log('Wireshark 安装器退出码 ' + IntToStr(ResultCode) + '，但没检测到安装');
  finally
    WizardForm.StatusLabel.Caption := '';
  end;
#endif
end;
