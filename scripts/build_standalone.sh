#!/usr/bin/env bash
# Build a standalone s1am binary for the current OS/architecture.
# Usage: ./scripts/build_standalone.sh [--output-dir DIR]
#
# Cross-platform notes:
#   macOS arm64  — run on Apple Silicon Mac
#   macOS x86_64 — run on Intel Mac
#   Linux x86_64 — run on Linux (or in CI on ubuntu-latest)
#   Windows      — run in Git Bash / WSL; output is s1am.exe
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# ── Detect platform label ────────────────────────────────────────────────────
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "$OS" in
  darwin)  PLATFORM="macos-${ARCH}" ;;
  linux)   PLATFORM="linux-${ARCH}" ;;
  mingw*|msys*|cygwin*) PLATFORM="windows-${ARCH}" ;;
  *)       PLATFORM="${OS}-${ARCH}" ;;
esac

# ── Optional --output-dir override ──────────────────────────────────────────
OUTPUT_DIR="release"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

echo "==> Building s1am for $PLATFORM → $OUTPUT_DIR/"

# ── Virtual env ─────────────────────────────────────────────────────────────
if [[ ! -d .venv-build ]]; then
  python3 -m venv .venv-build
fi

# Activate (bash/zsh on Unix; use Scripts/activate on Windows)
if [[ -f .venv-build/bin/activate ]]; then
  source .venv-build/bin/activate
elif [[ -f .venv-build/Scripts/activate ]]; then
  source .venv-build/Scripts/activate
fi

python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt pyinstaller --quiet

# ── PyInstaller ─────────────────────────────────────────────────────────────
pyinstaller \
  --clean \
  --noconfirm \
  --onefile \
  --name s1am \
  run_s1am.py

# ── Assemble release folder ──────────────────────────────────────────────────
mkdir -p "$OUTPUT_DIR"

# Binary (Windows gets .exe automatically from PyInstaller)
if [[ -f dist/s1am.exe ]]; then
  BIN_NAME="s1am-${PLATFORM}.exe"
  cp dist/s1am.exe "$OUTPUT_DIR/$BIN_NAME"
  cp dist/s1am.exe "$OUTPUT_DIR/s1am.exe"
else
  BIN_NAME="s1am-${PLATFORM}"
  cp dist/s1am "$OUTPUT_DIR/$BIN_NAME"
  cp dist/s1am "$OUTPUT_DIR/s1am"
  chmod +x "$OUTPUT_DIR/s1am" "$OUTPUT_DIR/$BIN_NAME"
fi

# Docs
cp README.md          "$OUTPUT_DIR/README.md"
cp .env.example       "$OUTPUT_DIR/.env.example"
cp RELEASE_NOTES.md   "$OUTPUT_DIR/RELEASE_NOTES.md"
cp USER_MANUAL.md     "$OUTPUT_DIR/USER_MANUAL.md"

# Shell completion scripts
mkdir -p "$OUTPUT_DIR/completions"
cp scripts/s1am-completion.bash "$OUTPUT_DIR/completions/"
cp scripts/s1am-completion.zsh  "$OUTPUT_DIR/completions/"

cat > "$OUTPUT_DIR/QUICKSTART.txt" << EOF
s1am standalone binary ($PLATFORM)
====================================

Quick start:
  export S1_BASE_URL="https://your-tenant.sentinelone.net"
  export S1_API_TOKEN="your_token"
  ./s1am list-accounts --format table

Shell completion:
  # bash
  source completions/s1am-completion.bash
  # zsh
  cp completions/s1am-completion.zsh /usr/local/share/zsh/site-functions/_s1am

Security:
  - Never put real credentials into .env.example
  - Enter token at runtime (hidden prompt) or use a named profile
  - See USER_MANUAL.md and RELEASE_NOTES.md for full details
EOF

# ── Optional zip ─────────────────────────────────────────────────────────────
ZIPNAME="s1am-${PLATFORM}.zip"
(cd "$OUTPUT_DIR" && zip -qr "../$ZIPNAME" .)
echo "==> Zip: $ROOT_DIR/$ZIPNAME"
echo "==> Done. Binary: $OUTPUT_DIR/$BIN_NAME"
