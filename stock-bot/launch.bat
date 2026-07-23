@echo off
cd /d "%~dp0"
set PYTHONTRADING_ROOT=%CD%

echo [WARN] launch.bat starts ONE bot only (EXE or run_all.py) — NOT your dual Live+Paper setup.
echo        For daily use, double-click: Start_Bot_and_Dashboard.bat
echo.
echo [INFO] Starting Trading Bot...
REM Dashboard auto-launch: set AUTO_LAUNCH_DASHBOARD=true in stock-bot\.env

if exist "dist\Weinstein-Trading-Bot.exe" (
    echo [INFO] Using frozen EXE: dist\Weinstein-Trading-Bot.exe
    "dist\Weinstein-Trading-Bot.exe"
    exit /b %ERRORLEVEL%
)

set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"

echo [INFO] Using source: %PY% run_all.py
"%PY%" run_all.py
