# 干净机测试脚本（v2.0.0）
# 临时备份 %LOCALAPPDATA%\Weishushu 和 %LOCALAPPDATA%\ms-playwright 和 ~\.weibo_book_cookies，
# 模拟"用户首次启动 EXE"的干净环境。
# -RestoreLatest 恢复原数据。
#
# B19 v1.2.0: 加备份 ~\.weibo_book_cookies（业务核心 weibo_book.login.DEFAULT_COOKIE_FILE 落点），
# frozen 模式下 cookie 注入的"主路径"在这；漏备的话恢复后会出现"明明有 cookie 却用不上"的诡异问题。
# 注意：Chrome DPAPI 路径（%LOCALAPPDATA%\Google\Chrome\User Data\...\Cookies）暂不清，
# 避免破坏用户 Chrome 数据（用户会骂）。

param(
    [switch]$RestoreLatest
)

$ErrorActionPreference = "Stop"

$backupDir = "$env:LOCALAPPDATA\Weishushu_CleanTest_Backup_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
$weishushuDir = "$env:LOCALAPPDATA\Weishushu"
$msPlaywrightDir = "$env:LOCALAPPDATA\ms-playwright"
$weiboBookCookies = Join-Path $env:USERPROFILE ".weibo_book_cookies"

function Backup-Path($src, $destName) {
    if (Test-Path $src) {
        $dest = "$backupDir\$destName"
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
        Write-Host "Backing up $src -> $dest"
        Move-Item $src $dest -Force
    } else {
        Write-Host "Skip (not exist): $src"
    }
}

function Restore-Path($srcName, $destPath) {
    $src = "$backupDir\$srcName"
    if (Test-Path $src) {
        if (Test-Path $destPath) {
            Remove-Item $destPath -Recurse -Force
        }
        Write-Host "Restoring $src -> $destPath"
        Move-Item $src $destPath -Force
    }
}

if ($RestoreLatest) {
    $latest = Get-ChildItem "$env:LOCALAPPDATA" -Filter "Weishushu_CleanTest_Backup_*" -Directory |
              Sort-Object Name -Descending | Select-Object -First 1
    if ($null -eq $latest) {
        Write-Host "No backup found in $env:LOCALAPPDATA\Weishushu_CleanTest_Backup_*"
        exit 1
    }
    $backupDir = $latest.FullName
    Write-Host "Restoring from $backupDir"
    Restore-Path "Weishushu" $weishushuDir
    Restore-Path "ms-playwright" $msPlaywrightDir
    Restore-Path ".weibo_book_cookies" $weiboBookCookies
    Remove-Item $backupDir -Recurse -Force
    Write-Host "Restore complete."
} else {
    Write-Host "=== Clean Test: backing up ==="
    Backup-Path $weishushuDir "Weishushu"
    Backup-Path $msPlaywrightDir "ms-playwright"
    Backup-Path $weiboBookCookies ".weibo_book_cookies"
    Write-Host ""
    Write-Host "Backup directory: $backupDir"
    Write-Host "Now you can test the EXE in a clean environment."
    Write-Host "When done, run: powershell -ExecutionPolicy Bypass -File scripts\clean_runtime_for_smoke.ps1 -RestoreLatest"
}
