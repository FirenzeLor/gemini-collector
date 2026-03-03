#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS_DIR="$PROJECT_DIR/scripts"
VENDOR_DIR="$SCRIPTS_DIR/_vendor"
DMG_OUT_DIR="$PROJECT_DIR/src-tauri/target/release/bundle/dmg"

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
echo "[1/4] 安装 Python 依赖到 _vendor/..."
rm -rf "$VENDOR_DIR"
"$PYTHON" -m pip install \
    --target "$VENDOR_DIR" \
    --quiet \
    --disable-pip-version-check \
    httpx \
    browser-cookie3
echo "      完成 ($(du -sh "$VENDOR_DIR" | cut -f1))"

# ── 2. 前端构建 ──────────────────────────────────────────────────────────────
echo "[2/4] 构建前端..."
npm run build --silent
echo "      完成"

# ── 3. Tauri 构建（生成 .app + .dmg）────────────────────────────────────────
echo "[3/4] 构建 Tauri 应用..."
cargo tauri build 2>&1 | grep -E "Compiling|Finished|Bundling|error" || true
echo "      完成"

# ── 4. 输出结果 ──────────────────────────────────────────────────────────────
echo "[4/4] 构建产物:"
if ls "$DMG_OUT_DIR"/*.dmg 2>/dev/null | head -1 | grep -q .; then
    ls -lh "$DMG_OUT_DIR"/*.dmg
    DMG_FILE="$(ls "$DMG_OUT_DIR"/*.dmg | head -1)"
    echo
    echo "DMG 路径: $DMG_FILE"
    # 可选：打开 Finder 定位到文件
    # open -R "$DMG_FILE"
else
    echo "未找到 DMG，请检查构建日志"
    echo "构建产物目录: $PROJECT_DIR/src-tauri/target/release/bundle/"
    ls "$PROJECT_DIR/src-tauri/target/release/bundle/" 2>/dev/null || true
    exit 1
fi
