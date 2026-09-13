@echo off
cd /d "%~dp0"
set PYTHONTRADING_ROOT=%CD%

echo [INFO] Launching PythonTrading Monitor...

REM Look-only sleeve caps so import config succeeds without remembering the env var.
REM Does not change Alpaca keys or start/stop trading.
if not defined PYTHONTRADING_ENV_FILE if exist "%~dp0.env.lookonly_dashboard" (
    set "PYTHONTRADING_ENV_FILE=%~dp0.env.lookonly_dashboard"
    echo [INFO] Using look-only env: %~dp0.env.lookonly_dashboard
)

set "PYW=pythonw"
if exist "%~dp0.venv\Scripts\pythonw.exe" set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%~dp0..\.venv\Scripts\pythonw.exe" set "PYW=%~dp0..\.venv\Scripts\pythonw.exe"

REM Source dashboard is the default so git pull gets close/logout and stamp
REM fixes without a Windows PyInstaller rebuild. Set DASHBOARD_USE_FROZEN=1
REM to launch dist\PythonTradingMonitor\PythonTradingMonitor.exe instead.
if /I not "%DASHBOARD_USE_FROZEN%"=="1" if /I not "%DASHBOARD_USE_FROZEN%"=="true" (
    if exist "dashboard_app.py" (
        echo [INFO] Starting source monitor: %PYW% dashboard_app.py
        start "" "%PYW%" "%~dp0dashboard_app.py"
        echo [INFO] Sign in when the window appears. Check logs\dashboard_crash.log if it closes.
        exit /b 0
    )
)

if exist "dist\PythonTradingMonitor\PythonTradingMonitor.exe" (
    start "" "dist\PythonTradingMonitor\PythonTradingMonitor.exe"
    echo [INFO] Started PythonTradingMonitor.exe
    echo [INFO] Sign in when the window appears. Check logs\dashboard_crash.log if it closes.
    exit /b 0
)

if exist "dashboard_app.py" (
    echo [INFO] Monitor EXE not found — using source: %PYW% dashboard_app.py
    start "" "%PYW%" "%~dp0dashboard_app.py"
    exit /b 0
)

echo [ERROR] No monitor found. Ensure dashboard_app.py exists.
pause
exit /b 1
