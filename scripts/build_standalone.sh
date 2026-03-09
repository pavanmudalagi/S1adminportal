#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

python3 -m venv .venv-build
source .venv-build/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller

pyinstaller \
  --clean \
  --noconfirm \
  --onefile \
  --name s1am \
  run_s1am.py

mkdir -p release
cp dist/s1am release/s1am
cp README.md release/README.md
cp .env.example release/.env.example
cp RELEASE_NOTES.md release/RELEASE_NOTES.md
cp USER_MANUAL.md release/USER_MANUAL.md

cat > release/QUICKSTART.txt << 'EOF'
Standalone CLI build created:
- ./s1am

Security note:
- Do NOT put real credentials into .env.example
- You can run ./s1am and enter token when prompted (hidden input)
- Or set S1_API_TOKEN only for the current shell session
- See RELEASE_NOTES.md and USER_MANUAL.md for full details
EOF

echo "Build complete. Share the folder: $ROOT_DIR/release"
