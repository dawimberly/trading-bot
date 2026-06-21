@echo off
cd /d "%~dp0"
set PYTHONTRADING_ROOT=%CD%

echo [INFO] Launching PythonTradingMonitor...
if exist "dist\PythonTradingMonitor\PythonTradingMonitor.exe" (
    start "" "dist\PythonTradingMonitor\PythonTradingMonitor.exe"
) else (
    echo [ERROR] Monitor EXE not found. Run build_dashboard.bat first.
    pause
)
