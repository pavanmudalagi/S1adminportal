# Release Notes

## v1.0.0 (2026-02-16)

### Highlights
- Added standalone executable packaging using PyInstaller.
- Added secure runtime token prompt (hidden input) when `S1_API_TOKEN` is not set.
- Added release artifact workflow and share-ready `release/` output.

### Included Features
- SentinelOne account operations:
  - `list-accounts`
  - `get-account`
  - `account-policy` (get/update)
  - `list-sites`
  - `list-agents`
  - `raw` endpoint invocation
- Cursor-based pagination support with `--all`.
- JSON response output for easy piping and automation.

### Security Improvements
- Tokens are read from environment variables or secure hidden prompt at runtime.
- No token persistence logic in app code.
- `.gitignore` updated to exclude common sensitive/local files:
  - `.env`, `.env.*` (except `.env.example`)
  - console export files, logs, build output directories

### Packaging and Distribution
- Added build script: `scripts/build_standalone.sh`
- Added entrypoint for bundling: `run_s1am.py`
- Standalone release bundle includes:
  - `s1am`
  - `README.md`
  - `.env.example` (placeholder only)
  - `QUICKSTART.txt`

### Dependency Notes
- Pinned `urllib3<2` to avoid LibreSSL warning on macOS environments using system Python.

### Known Limitations
- Current generated binary is macOS Apple Silicon (`arm64`) specific.
- For Windows/Linux/Intel macOS distribution, build on matching target OS/architecture.

