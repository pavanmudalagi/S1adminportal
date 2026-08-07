#!/usr/bin/env bash
# One-click launcher for the s1am web UI.
# Usage: ./scripts/start.sh [--port PORT] [--no-browser]
set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

# ── Virtual environment ────────────────────────────────────────────────────
if [ -d ".venv" ]; then
  source .venv/bin/activate
elif [ -d "venv" ]; then
  source venv/bin/activate
else
  echo "No virtual environment found. Creating .venv …"
  python3 -m venv .venv
  source .venv/bin/activate
fi

# ── Python path (src layout) ────────────────────────────────────────────────
export PYTHONPATH="$REPO_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

# ── Dependencies ────────────────────────────────────────────────────────────
python3 -c "import flask" 2>/dev/null || {
  echo "Installing dependencies …"
  pip install -q -r requirements.txt
}

# ── Launch ──────────────────────────────────────────────────────────────────
echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║  s1am  —  SentinelOne Account Mgr   ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

exec python3 run_web.py "$@"
