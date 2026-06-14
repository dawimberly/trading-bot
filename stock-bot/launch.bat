@echo off
REM PythonTrading — one-click launcher (dashboard + trading bot)
REM Double-click this file or create a desktop shortcut to it.

cd /d "%~dp0"
set "PYTHONTRADING_ROOT=%CD%"

set "PYW=.venv\Scripts\pythonw.exe"
if not exist "%PYW%" if exist "..\.venv\Scripts\pythonw.exe" set "PYW=..\.venv\Scripts\pythonw.exe"

if not exist "%PYW%" (
    echo [ERROR] Virtual environment not found.
    echo Run friend_setup.bat in stock-bot, or from repo root:
    echo   cd stock-bot
    echo   python -m venv .venv
    echo   pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist ".env" if exist "..\.env" set "PYTHONTRADING_ENV_FILE=%~dp0..\.env"
if not exist ".env" if not defined PYTHONTRADING_ENV_FILE (
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

start "" "%PYW%" dashboard_app.py --launch-bot 1>>"%LAUNCH_LOG%" 2>&1

if errorlevel 1 (
    echo Dashboard failed to start. See %LAUNCH_LOG%
    pause
    exit /b 1
)

echo Dashboard started. Log: %LAUNCH_LOG%
