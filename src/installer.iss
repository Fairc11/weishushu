; FILE MUST BE SAVED AS UTF-8 WITH BOM —— 否则 Inno Setup 编译器对中文 MsgBox 会乱码
; v2.0.0 Inno Setup 脚本（抖音 v1.4.2 范式）
; 功能：
;   - 安装到 {autopf}\Weishushu
;   - 桌面快捷方式 + 开始菜单
;   - 卸载弹"是否清理用户数据"选项（D2）
;   - ChineseSimplified Messages 兜底（B08 v1.2.0: Languages chs 替换 default）
;   - icon.ico 品牌色（v1.1.6 新增）
;   - 卸载 [Code] 段兼容 v1.1.5/6 索引文件
;   - B06 v1.2.0: MyAppVersion 通过 build_exe.bat 命令行 -DMyAppVersion 传入
;     默认 fallback 2.0.0（与 backend/app/version.py VERSION 保持一致）

#ifndef MyAppVersion
  #define MyAppVersion "2.0.1"
#endif

#define MyAppName "微书薯"
#define MyAppNameEn "Weishushu"
#define MyAppPublisher "Weishushu Project"
#define MyAppURL "https://github.com/yourname/weibo-book"
#define MyAppExeName "Weishushu.exe"

[Setup]
; 注意：SetupIconFile 注释掉，缺 icon.ico 也不会失败
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppNameEn}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=Weishushu_Setup_v{#MyAppVersion}
SetupIconFile=icon.ico
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} v{#MyAppVersion}
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany={#MyAppPublisher}
AppCopyright=Copyright (C) 2026 {#MyAppPublisher}
WizardStyle=modern
WizardSizePercent=110

[Languages]
; B08 v1.2.0 退回：IS 6.7.3 默认未装 ChineseSimplified.isl（需单独下载）
; 装包向导走英文（Default.isl），但 [Code] 段 MsgBox 自定义中文仍生效
; 如要中文向导，需手动从 https://jrsoftware.org/files/istrans/ 下载 ChineseSimplified-6.7.3.isl
;   放到 IS Languages 目录后改回: Name: "chs"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "default"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkablealone

[Files]
; PyInstaller 输出的 onedir 目录（含 _internal/）
Source: "dist\Weishushu\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 注意：不要 Flags: "restartreplace" 否则 Win10 启动时会被杀

[Icons]
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Dirs]
Name: "{app}"

[Code]
// v1.1.4 D2：卸载时弹"是否清理"；自动化参数只影响隔离应用数据，不删全局浏览器缓存。
// 用户数据根由 GetUserDataRoot() 单一来源决定：
// 环境变量 LOCALAPPDATA 存在时与应用运行时同源；缺失时回退 {localappdata}。
var
  CleanupResult: Boolean;

function GetUserDataRoot(): String;
begin
  Result := ExpandConstant(
    '{%LOCALAPPDATA|{localappdata}}\{#MyAppNameEn}'
  );
end;

function IsKeepUserDataParam(): Boolean;
begin
  Result := ExpandConstant('{param:KeepUserData|0}') = '1';
end;

function IsDeleteUserDataParam(): Boolean;
begin
  Result := ExpandConstant('{param:DeleteUserData|0}') = '1';
end;

function InitializeUninstall(): Boolean;
begin
  if IsKeepUserDataParam() and IsDeleteUserDataParam() then
  begin
    MsgBox(
      '【微书薯 v{#MyAppVersion} 卸载参数冲突】' + #13#10 +
      '/KEEPUSERDATA=1 与 /DELETEUSERDATA=1 不能同时使用。',
      mbError, MB_OK
    );
    Result := False;
    Exit;
  end;

  if IsKeepUserDataParam() then
  begin
    CleanupResult := False;
    Result := True;
    Exit;
  end;

  if IsDeleteUserDataParam() then
  begin
    CleanupResult := True;
    Result := True;
    Exit;
  end;

  // 中文弹窗（D2 + D1 抖音范式）
  if MsgBox(
    '【微书薯 v{#MyAppVersion} 卸载提示】' + #13#10 +
    '' + #13#10 +
    '是否同时删除以下内容？' + #13#10 +
    '' + #13#10 +
    '✓ 用户数据 ' + GetUserDataRoot() + #13#10 +
    '   （设置、日志与运行状态）' + #13#10 +
    '✓ 登录凭证 {%USERPROFILE}\.weibo_book_cookies' + #13#10 +
    '' + #13#10 +
    '你另行保存的微博书档案目录不会被删除。' + #13#10 +
    '点「否」仅卸载程序文件，保留上述数据以便日后恢复。',
    mbConfirmation, MB_YESNO
  ) = IDYES then
    CleanupResult := True
  else
    CleanupResult := False;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if not CleanupResult then Exit;
  if CurUninstallStep = usUninstall then
  begin
    // 删除隔离应用数据；不删除全局 %LOCALAPPDATA%\ms-playwright。
    DelTree(GetUserDataRoot(), True, True, True);
    // 登录凭证在用户主目录而非 LOCALAPPDATA，需单独删除；
    // 用户自选的微博书档案目录不属于此处，卸载不触碰。
    DeleteFile(ExpandConstant('{%USERPROFILE}\.weibo_book_cookies'));
  end;
end;
