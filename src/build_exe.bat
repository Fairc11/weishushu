@echo off
REM v2.0.0 一键打包脚本（抖音 v1.4.2 范式）
REM 流程：kill 旧进程 → 装 Playwright Chromium → 跑 release_check → PyInstaller → Inno Setup
REM B06 v1.2.0: MyAppVersion 从 backend/app/version.py 读，与 js_api.py / installer.iss 保持唯一源

setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 > nul

echo === 阶段 0：环境自检 ===
where pyinstaller >nul 2>&1
if errorlevel 1 (
  echo [ERROR] PyInstaller 未安装
  echo 1^> pip install pyinstaller==6.20.0
  exit /b 1
)

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python 不在 PATH
  exit /b 1
)

echo === 阶段 1：Kill 旧进程 ===
taskkill /F /IM Weishushu.exe 2>nul
timeout /t 2 /nobreak > nul 2>nul

echo === 阶段 2：装 Playwright Chromium（D1 烘焙） ===
python -m playwright install chromium
if errorlevel 1 (
  echo [ERROR] playwright install 失败
  exit /b 1
)

echo === 阶段 3：跑 release_check.py（20 项硬约束） ===
python scripts\release_check.py
if errorlevel 1 (
  echo [ERROR] release_check 失败——修复后才能打包
  exit /b 1
)

echo === 阶段 4：清理旧 build/dist ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo === 阶段 5：跑 PyInstaller onedir ===
pyinstaller build.spec --clean
if errorlevel 1 (
  echo [ERROR] PyInstaller 失败——看 build/warn-Weishushu.txt
  exit /b 1
)

REM 验证 EXE 真的生成了
if not exist "dist\Weishushu\Weishushu.exe" (
  echo [ERROR] Weishushu.exe 未生成
  exit /b 1
)

echo === 阶段 5.5：写构建清单 ===
python scripts\write_build_manifest.py --root dist\Weishushu\_internal --output dist\Weishushu\_internal\weishushu_build_manifest.json --platform win32 --architecture x64 --profile user --executable-name Weishushu.exe --bundle-id com.weishushu.desktop --dependency-lock requirements\lock-windows-x64.txt --resource-dir backend\app\templates --resource-dir backend\app\static --resource-dir weibo_book\templates
if errorlevel 1 (
  echo [ERROR] 构建清单生成失败
  exit /b 1
)

echo === 阶段 6：跑 Inno Setup（可选 — 本机装了 IS 才跑） ===
set "ISCC="
for /f "delims=" %%i in ('where /R "%LOCALAPPDATA%\Programs\Inno Setup 6" ISCC.exe 2^>nul') do set "ISCC=%%i"
for /f "delims=" %%i in ('where /R "%ProgramFiles%\Inno Setup 6" ISCC.exe 2^>nul') do set "ISCC=%%i"
if defined ISCC (
  echo [IS] Found: %ISCC%
  REM B06 v1.2.0: 从 backend/app/version.py 读 VERSION 作为 MyAppVersion 传入
  REM 走 scripts\read_version.py（避免 cmd 嵌套引号地狱）
  set "MY_VER=2.0.1"
  for /f "delims=" %%v in ('python scripts\read_version.py') do set "MY_VER=%%v"
  echo [IS] MyAppVersion=!MY_VER!
  "%ISCC%" /DMyAppVersion=!MY_VER! installer.iss
  if errorlevel 1 (
    echo [ERROR] Inno Setup 编译失败
    exit /b 1
  )
  echo.
  echo === [Done] 安装包：installer\Weishushu_Setup_v!MY_VER!.exe ===
) else (
  echo.
  echo === [Done] onedir 目录：dist\Weishushu\ ===
  echo   （本机未装 Inno Setup，跳过安装包编译。装好后重跑本脚本即可）
)

echo.
if not "%PTU_NO_PAUSE%"=="1" pause
