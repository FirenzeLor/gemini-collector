#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS_DIR="$PROJECT_DIR/scripts"
VENDOR_DIR="$SCRIPTS_DIR/_vendor"
BUNDLE_DIR="$PROJECT_DIR/src-tauri/target/release/bundle"
APP_NAME="gemini-mac-app"
APP_PATH="$BUNDLE_DIR/macos/$APP_NAME.app"
RESOURCES_DIR="$APP_PATH/Contents/Resources"

# 确保 Cargo 在 PATH 中
export PATH="$HOME/.cargo/bin:$PATH"

# Python 可执行文件（与 lib.rs 中的查找顺序一致）
find_python() {
    for py in /usr/local/bin/python3 /opt/homebrew/bin/python3 /usr/bin/python3; do
        if [ -x "$py" ]; then echo "$py"; return; fi
    done
    echo "python3"
}
PYTHON="$(find_python)"

echo "=== Gemini Collector DMG 构建 ==="
echo "Python: $PYTHON ($($PYTHON --version))"
echo "项目目录: $PROJECT_DIR"
echo

# ── 1. Vendor Python 依赖 ────────────────────────────────────────────────────
# macOS 运行时依赖：
#   httpx → anyio, certifi, httpcore, h11, idna, sniffio
#   browser-cookie3 → lz4, pycryptodomex (Cryptodome)
#   dbus-python / jeepney / shadowcopy 仅 Linux/Windows 需要，跳过
VENDOR_PACKAGES=(
    httpx anyio certifi httpcore h11 idna sniffio
    browser_cookie3 lz4 Crypto Cryptodome
)

echo "[1/5] 构建 Python vendor 目录..."
rm -rf "$VENDOR_DIR"
mkdir -p "$VENDOR_DIR"

SITE_PACKAGES="$("$PYTHON" -c 'import site; print(site.getsitepackages()[0])')"

# 先尝试 pip install；若 SSL 失败则从本机 site-packages 复制
if "$PYTHON" -m pip install \
    --target "$VENDOR_DIR" \
    --quiet \
    --disable-pip-version-check \
    httpx \
    browser-cookie3 2>/dev/null; then
    echo "      pip install 成功"
else
    echo "      pip 不可用，从本机 site-packages 复制..."
    for pkg in "${VENDOR_PACKAGES[@]}"; do
        src="$SITE_PACKAGES/$pkg"
        if [ -d "$src" ]; then
            cp -r "$src" "$VENDOR_DIR/"
        else
            echo "      警告: 未找到 $pkg，跳过"
        fi
    done
fi
echo "      完成 ($(du -sh "$VENDOR_DIR" | cut -f1))"

# ── 2. 前端构建 ──────────────────────────────────────────────────────────────
echo "[2/5] 构建前端..."
npm run build --silent
echo "      完成"

# ── 3. Tauri 构建 .app（跳过 DMG，后面手动制作）────────────────────────────
echo "[3/5] 构建 Tauri .app..."
npx tauri build --bundles app 2>&1 | grep -E "^   (Compiling|Finished|Bundling|error)" || true
echo "      完成: $APP_PATH"

# ── 4. 注入 _vendor 到 .app/Contents/Resources ───────────────────────────────
echo "[4/5] 注入 _vendor 到 app bundle..."
cp -r "$VENDOR_DIR" "$RESOURCES_DIR/_vendor"
echo "      完成 ($(du -sh "$RESOURCES_DIR/_vendor" | cut -f1))"

# 验证关键包是否在正确位置
for pkg in httpx browser_cookie3 anyio; do
    if [ -d "$RESOURCES_DIR/_vendor/$pkg" ]; then
        echo "      ✓ $pkg"
    else
        echo "      ✗ $pkg 未找到，构建可能有问题"
    fi
done

# ── 5. 制作 DMG ──────────────────────────────────────────────────────────────
echo "[5/5] 制作 DMG..."
mkdir -p "$BUNDLE_DIR/dmg"

PRODUCT_NAME="$APP_NAME"
VERSION="$(plutil -p "$APP_PATH/Contents/Info.plist" | grep CFBundleShortVersionString | awk -F'"' '{print $4}')"
ARCH="$(uname -m)"
DMG_NAME="${PRODUCT_NAME}_${VERSION}_${ARCH}.dmg"
DMG_PATH="$BUNDLE_DIR/dmg/$DMG_NAME"

# 使用 hdiutil 制作带 Applications 快捷方式的 DMG
TMP_DMG_DIR="$(mktemp -d)"
cp -r "$APP_PATH" "$TMP_DMG_DIR/"
ln -s /Applications "$TMP_DMG_DIR/Applications"

hdiutil create \
    -volname "$PRODUCT_NAME" \
    -srcfolder "$TMP_DMG_DIR" \
    -ov \
    -format UDZO \
    "$DMG_PATH" 2>/dev/null

rm -rf "$TMP_DMG_DIR"

echo
echo "=== 构建完成 ==="
ls -lh "$DMG_PATH"
echo "DMG 路径: $DMG_PATH"
