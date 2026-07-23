@echo off
cd /d "%~dp0"
set PYTHONTRADING_ROOT=%CD%

echo [INFO] Stopping dashboard (PythonTradingMonitor + dashboard_app)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_dashboard.ps1"
echo.
pause
