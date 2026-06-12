@echo off
REM PythonTrading — launch the PyInstaller monitor (.exe) from project root.
REM Use this for desktop shortcuts (not the raw .exe in dist\).

cd /d "%~dp0"
set "PYTHONTRADING_ROOT=%CD%"

set "MONITOR_EXE=dist\PythonTradingMonitor\PythonTradingMonitor.exe"
if not exist "%MONITOR_EXE%" (
    echo [ERROR] Monitor exe not found: %MONITOR_EXE%
    echo Run build_dashboard.bat to rebuild it.
    pause
    exit /b 1
)

if not exist "logs" mkdir logs

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dashboard_running.ps1"
if errorlevel 1 (
    echo.
    echo Dashboard is already running. Check the taskbar or system tray ^(near the clock^).
    echo To restart: run stop_dashboard.bat, then try again.
    pause
    exit /b 0
)

for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%t"
set "LAUNCH_LOG=logs\monitor_%STAMP%.log"

start "" "%MONITOR_EXE%" --launch-bot 1>>"%LAUNCH_LOG%" 2>&1

timeout /t 2 /nobreak >nul
tasklist /FI "IMAGENAME eq PythonTradingMonitor.exe" 2>nul | find /I "PythonTradingMonitor.exe" >nul
if errorlevel 1 (
    echo.
    echo Monitor failed to start. See %LAUNCH_LOG%
    if exist "%LAUNCH_LOG%" type "%LAUNCH_LOG%"
    pause
    exit /b 1
)

echo PythonTrading Monitor started.
