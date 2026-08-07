@echo off
REM One-click launcher for the s1am web UI (Windows).
REM Usage: scripts\start.bat [--port PORT] [--no-browser]

cd /d "%~dp0.."

REM ── Virtual environment ────────────────────────────────────────────────────
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
) else if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
) else (
    echo No virtual environment found. Creating .venv ...
    python -m venv .venv
    call .venv\Scripts\activate.bat
)

REM ── Dependencies ────────────────────────────────────────────────────────────
python -c "import flask" 2>NUL || (
    echo Installing dependencies ...
    pip install -q -r requirements.txt
)

REM ── Launch ──────────────────────────────────────────────────────────────────
echo.
echo   s1am  --  SentinelOne Account Manager
echo.

python run_web.py %*
