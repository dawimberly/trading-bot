@echo off
cd /d "%~dp0"
set PYTHONTRADING_ROOT=%CD%

echo [INFO] Launching PythonTrading Monitor...

if exist "dist\PythonTradingMonitor\PythonTradingMonitor.exe" (
    start "" "dist\PythonTradingMonitor\PythonTradingMonitor.exe"
    echo [INFO] Started PythonTradingMonitor.exe
    echo [INFO] Sign in when the window appears. Check logs\dashboard_crash.log if it closes.
    exit /b 0
)

set "PYW=pythonw"
if exist "%~dp0.venv\Scripts\pythonw.exe" set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%~dp0..\.venv\Scripts\pythonw.exe" set "PYW=%~dp0..\.venv\Scripts\pythonw.exe"

if exist "dashboard_app.py" (
    echo [INFO] Monitor EXE not found — using source: %PYW% dashboard_app.py
    start "" "%PYW%" "%~dp0dashboard_app.py"
    exit /b 0
)

echo [ERROR] No monitor found. Build with build_dashboard.bat or ensure dashboard_app.py exists.
pause
exit /b 1
