@echo off
REM PythonTrading — one-click launcher (dashboard + trading bot)
REM Double-click this file or create a desktop shortcut to it.

cd /d "%~dp0"
set "PYTHONTRADING_ROOT=%CD%"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERROR] Virtual environment not found.
    echo Run from project root:
    echo   python -m venv .venv
    echo   .\.venv\Scripts\Activate.ps1
    echo   pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist ".env" (
    echo [WARN] No .env file — the dashboard setup wizard will prompt on first launch.
)

if not exist "logs" mkdir logs

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dashboard_running.ps1"
if errorlevel 1 (
    echo Close the existing window first, or run stop_dashboard.bat
    pause
    exit /b 0
)

for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%t"
set "LAUNCH_LOG=logs\dashboard_%STAMP%.log"

start "" ".venv\Scripts\pythonw.exe" dashboard_app.py --launch-bot 1>>"%LAUNCH_LOG%" 2>&1

if errorlevel 1 (
    echo Dashboard failed to start. See %LAUNCH_LOG%
    pause
    exit /b 1
)

echo Dashboard started. Log: %LAUNCH_LOG%
