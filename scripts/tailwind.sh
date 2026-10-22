#!/usr/bin/env sh
# Tailwind CSS Standalone CLI（Node.js 不要）で app/static/css/app.css を生成する。
#   scripts/tailwind.sh            # 1回ビルド（minify）
#   scripts/tailwind.sh --watch    # 開発時の監視ビルド
set -eu

TAILWIND_VERSION="v4.3.3"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT_DIR/bin/tailwindcss-$TAILWIND_VERSION"

if [ ! -x "$BIN" ]; then
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64) TARGET="linux-x64" ;;
        Linux-aarch64) TARGET="linux-arm64" ;;
        Darwin-x86_64) TARGET="macos-x64" ;;
        Darwin-arm64) TARGET="macos-arm64" ;;
        *) echo "unsupported platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
    esac
    mkdir -p "$ROOT_DIR/bin"
    curl -fsSL -o "$BIN" \
        "https://github.com/tailwindlabs/tailwindcss/releases/download/$TAILWIND_VERSION/tailwindcss-$TARGET"
    chmod +x "$BIN"
fi

if [ "${1:-}" = "--watch" ]; then
    exec "$BIN" -i "$ROOT_DIR/app/static/css/input.css" -o "$ROOT_DIR/app/static/css/app.css" --watch
fi
exec "$BIN" -i "$ROOT_DIR/app/static/css/input.css" -o "$ROOT_DIR/app/static/css/app.css" --minify
