#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "[ERROR] scripts/build_dmg.sh 只能在 macOS 上运行。"
  exit 1
fi

# 构建身份失败关闭：忽略调用环境遗留值，只有显式 --dev 才启用开发 profile。
unset WEISHUSHU_PROFILE

if [[ "$#" -gt 1 ]]; then
  echo "[ERROR] 只能指定一个构建身份参数。"
  echo "[INFO] 用法: scripts/build_dmg.sh [--user|--dev]"
  exit 1
fi

# v2.0.0 阶段 3：支持 --dev 切换开发版（独立 DMG 名 + 卷名）
DEV_MODE=0
BUNDLE_NAME="Weishushu"
VOLNAME="微书薯"
BUILD_COMMAND="scripts/build_mac.sh"
for arg in "$@"; do
  case "$arg" in
    --dev)
      DEV_MODE=1
      BUNDLE_NAME="WeishushuDev"
      VOLNAME="微书薯 Dev"
      BUILD_COMMAND="scripts/build_mac.sh --dev"
      export WEISHUSHU_PROFILE="dev"
      ;;
    --user)
      DEV_MODE=0
      BUNDLE_NAME="Weishushu"
      VOLNAME="微书薯"
      BUILD_COMMAND="scripts/build_mac.sh"
      unset WEISHUSHU_PROFILE
      ;;
    *)
      echo "[ERROR] 未知参数: $arg"
      echo "[INFO] 用法: scripts/build_dmg.sh [--user|--dev]"
      exit 1
      ;;
  esac
done

PYTHON="${PYTHON:-.venv/bin/python}"
APP="dist/$BUNDLE_NAME.app"
EXECUTABLE="$APP/Contents/MacOS/$BUNDLE_NAME"

if [[ ! -d "$APP" || ! -f "$EXECUTABLE" ]]; then
  echo "[ERROR] 缺少 $APP，请先运行 $BUILD_COMMAND。"
  exit 1
fi

if ! file "$EXECUTABLE" | grep -Fq "Mach-O 64-bit executable arm64"; then
  echo "[ERROR] $BUNDLE_NAME.app 不是 Apple Silicon arm64 构建。"
  file "$EXECUTABLE"
  exit 1
fi

VERSION="$($PYTHON scripts/read_version.py)"
if [[ "$DEV_MODE" -eq 1 ]]; then
  OUTPUT_NAME="WeishushuDev-v${VERSION}-macOS-arm64.dmg"
else
  OUTPUT_NAME="Weishushu-v${VERSION}-macOS-arm64.dmg"
fi
OUTPUT="dist/$OUTPUT_NAME"
CHECKSUM="$OUTPUT.sha256"
STAGING="build/dmg-root"
MOUNT_POINT="$(mktemp -d /tmp/weishushu-dmg.XXXXXX)"
MOUNTED=0

cleanup() {
  if [[ "$MOUNTED" -eq 1 ]]; then
    hdiutil detach "$MOUNT_POINT" -quiet || true
  fi
  rm -rf "$MOUNT_POINT"
}
trap cleanup EXIT

codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

rm -rf "$STAGING" "$OUTPUT" "$CHECKSUM"
mkdir -p "$STAGING"
ditto "$APP" "$STAGING/$BUNDLE_NAME.app"
ln -s /Applications "$STAGING/Applications"

hdiutil create \
  -volname "$VOLNAME" \
  -srcfolder "$STAGING" \
  -format UDZO \
  -ov \
  "$OUTPUT"

hdiutil attach "$OUTPUT" -nobrowse -readonly -mountpoint "$MOUNT_POINT" -quiet
MOUNTED=1
test -d "$MOUNT_POINT/$BUNDLE_NAME.app"
test -L "$MOUNT_POINT/Applications"
file "$MOUNT_POINT/$BUNDLE_NAME.app/Contents/MacOS/$BUNDLE_NAME" | grep -Fq "Mach-O 64-bit executable arm64"
codesign --verify --deep --strict "$MOUNT_POINT/$BUNDLE_NAME.app"
hdiutil detach "$MOUNT_POINT" -quiet
MOUNTED=0

(
  cd dist
  shasum -a 256 "$OUTPUT_NAME" > "$OUTPUT_NAME.sha256"
)

echo "[OK] DMG: $OUTPUT"
echo "[OK] SHA-256: $CHECKSUM"
