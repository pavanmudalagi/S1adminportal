# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

**s1am** is a Python CLI and web application for automating SentinelOne account management via the SentinelOne MGMT API v2.1. It exposes both a `s1am` CLI and a browser-based single-page UI built on Flask.

## Commands

```bash
# Install dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run CLI
python -m s1am list-accounts
python -m s1am --profile acme list-agents --all

# Run web UI (opens browser at http://127.0.0.1:5173)
python run_web.py                       # run_web.py auto-adds src/ to sys.path
./scripts/start.sh [--port 8080] [--no-browser]   # sets PYTHONPATH=src automatically

# Run tests
PYTHONPATH=src pytest tests/

# Run a single test
PYTHONPATH=src pytest tests/test_cli.py::TestFormatTable

# Build standalone binary (macOS/Linux/Windows)
./scripts/build_standalone.sh [--output-dir DIR]
```

## Configuration

The tool reads credentials from (in priority order): environment variables → `~/.config/s1am/config` profile file → interactive prompt at runtime.

Key environment variables (see `.env.example`):
- `S1_BASE_URL` — required, e.g. `https://your-tenant.sentinelone.net`
- `S1_API_TOKEN` — required (or prompted at runtime via `getpass`)
- `S1_AUTH_PREFIX` — default empty; set to `APIToken` if needed by your tenant
- `S1_API_PATH` — default `/web/api/v2.1`
- `S1_POWERQUERY_TOKEN` — optional, for metering/usage reports

## Architecture

### Core Layers

**`src/s1am/api/config.py` → `src/s1am/api/client.py` → `src/s1am/cli.py`**

- **`Config`** (`src/s1am/api/config.py`) — loads env vars and profile files; profile files live at `~/.config/s1am/config`
- **`SentinelOneClient`** (`src/s1am/api/client.py`) — thin `requests` wrapper; builds URLs, sets auth headers, handles cursor-based pagination via `paginate()` (follows `nextCursor` or `next_cursor` fields), raises `SentinelOneError` on API errors
- **`cli.py`** (`src/s1am/cli.py`) — `argparse`-based CLI; output formatters (`_format_table`, `_format_csv`) and response extraction helpers (`_extract_rows`, `_flatten_pages`) live here

### Web UI (`src/s1am/components/app.py` + `templates/index.html`)

The web layer is a thin Flask wrapper that **invokes the CLI as a subprocess** rather than calling Python functions directly. This keeps the web and CLI layers independent.

Key design points:
- `POST /api/run` receives a command ID + form data, calls `build_args()` on the command registry entry to produce CLI arguments, then runs `subprocess.run(s1am ...)` with a 180-second timeout
- The **command registry** (~40 commands) in `src/s1am/components/app.py` defines every command available in the UI: its form fields, which credentials it requires (`"mgmt"`, `"pq"`, or `"none"`), and a `build_args` lambda that maps form values to CLI flags
- Session credentials are stored in Flask's signed cookie session; profile switching persists to the session
- Profile display names are saved to `~/.config/s1am/web_labels.json`
- Flask secret key is generated once and stored at `~/.config/s1am/web_secret.key` (mode 0o600)

### Adding a New Command

1. Add the CLI subparser and handler in `src/s1am/cli.py`
2. Add a corresponding entry in the command registry in `src/s1am/components/app.py` (category, fields, `build_args` lambda)
3. Add unit tests for any new helper functions in `tests/test_cli.py`

### API Reference

All reference docs are in `docs/`:
- `docs/cheatsheet.md` — most-used endpoints (start here)
- `docs/api_2.1.md` — full SentinelOne MGMT API v2.1 docs (6.3 MB)
- `docs/swagger_2.1.md` — OpenAPI spec (13.9 MB)
- `docs/update-usage-metering.md` — metering/PowerQuery API docs

The large files should be searched, not read in full.
