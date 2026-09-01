#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "[ERROR] scripts/build_mac.sh 只能在 macOS 上运行。"
  exit 1
fi

# 构建身份失败关闭：忽略调用环境遗留值，只有显式 --dev 才启用开发 profile。
unset WEISHUSHU_PROFILE

if [[ "$#" -gt 1 ]]; then
  echo "[ERROR] 只能指定一个构建身份参数。"
  echo "[INFO] 用法: scripts/build_mac.sh [--user|--dev]"
  exit 1
fi

# v2.0.0 阶段 3：支持 --dev 切换开发版（独立 Bundle ID + 数据目录 + 显示名）
DEV_MODE=0
BUNDLE_NAME="Weishushu"
for arg in "$@"; do
  case "$arg" in
    --dev)
      DEV_MODE=1
      BUNDLE_NAME="WeishushuDev"
      export WEISHUSHU_PROFILE="dev"
      ;;
    --user)
      DEV_MODE=0
      BUNDLE_NAME="Weishushu"
      unset WEISHUSHU_PROFILE
      ;;
    *)
      echo "[ERROR] 未知参数: $arg"
      echo "[INFO] 用法: scripts/build_mac.sh [--user|--dev]"
      exit 1
      ;;
  esac
done

if [[ "$DEV_MODE" -eq 1 ]]; then
  echo "[INFO] 构建开发版（Bundle ID: com.weishushu.desktop.dev, 数据目录: WeishushuDev）"
else
  echo "[INFO] 构建用户版（Bundle ID: com.weishushu.desktop, 数据目录: Weishushu）"
fi

PYTHON="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "[ERROR] 找不到 Python: $PYTHON"
  echo "[INFO] 先执行: /path/to/python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi

# 构建前只读校验：当前 Python 环境必须与 macos-arm64 完整锁一致。
"$PYTHON" scripts/verify_macos_lock.py
if [[ $? -ne 0 ]]; then
  echo "[ERROR] 当前环境与 requirements/lock-macos-arm64.txt 不一致，停止构建"
  exit 1
fi

if ! "$PYTHON" -m PyInstaller --version >/dev/null 2>&1; then
  echo "[ERROR] 当前环境未安装 PyInstaller。"
  echo "[INFO] 执行: $PYTHON -m pip install -r requirements.txt"
  exit 1
fi

# 构建源码身份门禁：只允许从干净提交构建；产物清单记录当前 HEAD。
DIRTY="$(git status --porcelain 2>/dev/null || true)"
if [[ -n "$DIRTY" ]]; then
  echo "[ERROR] 工作树存在未提交修改，拒绝构建。先提交或暂存全部修改："
  echo "$DIRTY"
  exit 1
fi
SOURCE_COMMIT="$(git rev-parse HEAD 2>/dev/null || true)"
if [[ -z "$SOURCE_COMMIT" ]]; then
  echo "[ERROR] 无法读取 git HEAD，拒绝构建。"
  exit 1
fi
export WEISHUSHU_SOURCE_COMMIT="$SOURCE_COMMIT"
echo "[INFO] 构建源码提交: $SOURCE_COMMIT"

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/Library/Caches/ms-playwright}"
"$PYTHON" -m playwright install chromium

rm -rf "build/$BUNDLE_NAME" "dist/$BUNDLE_NAME.app"
"$PYTHON" -m PyInstaller --clean --noconfirm build_mac.spec

if [[ ! -d "dist/$BUNDLE_NAME.app" ]]; then
  echo "[ERROR] 未生成 dist/$BUNDLE_NAME.app"
  exit 1
fi

# 生成构建清单（Mac 资源根为 .app/Contents/Resources）
if [[ "$DEV_MODE" -eq 1 ]]; then
  PROFILE="dev"
  EXECUTABLE_NAME="WeishushuDev"
  BUNDLE_ID="com.weishushu.desktop.dev"
else
  PROFILE="user"
  EXECUTABLE_NAME="Weishushu"
  BUNDLE_ID="com.weishushu.desktop"
fi
"$PYTHON" scripts/write_build_manifest.py \
  --root "dist/$BUNDLE_NAME.app/Contents/Resources" \
  --output "dist/$BUNDLE_NAME.app/Contents/Resources/weishushu_build_manifest.json" \
  --platform darwin --architecture arm64 --profile "$PROFILE" \
  --executable-name "$EXECUTABLE_NAME" --bundle-id "$BUNDLE_ID" \
  --dependency-lock requirements/lock-macos-arm64.txt \
  --resource-dir backend/app/templates --resource-dir backend/app/static \
  --resource-dir weibo_book/templates \
  --browser-archive playwright-browsers.tar.gz

# 清单写入 Bundle 后重新执行签名与核验；未使用 Developer ID 时仍做 ad-hoc 签名验证。
if command -v codesign >/dev/null 2>&1; then
  codesign --force --deep --sign - "dist/$BUNDLE_NAME.app"
  codesign --verify --deep --strict "dist/$BUNDLE_NAME.app"
fi

echo "[OK] Mac app 已生成: dist/$BUNDLE_NAME.app"
