#!/usr/bin/env python3
"""
One-click launcher for the s1am web UI.

Usage:
  python run_web.py                # start on http://127.0.0.1:5173
  python run_web.py --port 8080    # custom port
  python run_web.py --no-browser   # start without opening the browser
"""
import argparse
import sys
from pathlib import Path

# Ensure src/ is on the path when run directly (outside of `pip install -e .`)
_src = Path(__file__).resolve().parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))


def main():
    parser = argparse.ArgumentParser(description="Launch the s1am web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5173, help="Port (default: 5173)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    args = parser.parse_args()

    try:
        from s1am.components.app import run
    except ImportError as exc:
        print(f"Error: {exc}")
        print("Install web dependencies first:  pip install flask")
        sys.exit(1)

    print(f"  s1am web UI  →  http://{args.host}:{args.port}")
    print("  Press Ctrl+C to stop.\n")

    run(host=args.host, port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
