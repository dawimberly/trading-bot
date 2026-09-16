@echo off
cd /d "%~dp0"
set PYTHONTRADING_ROOT=%CD%

if not defined PYTHONTRADING_ENV_FILE if exist "%~dp0.env.lookonly_dashboard" (
    set "PYTHONTRADING_ENV_FILE=%~dp0.env.lookonly_dashboard"
)

set "PYW=pythonw"
if exist "%~dp0..\venv311\Scripts\pythonw.exe" set "PYW=%~dp0..\venv311\Scripts\pythonw.exe"
if exist "%~dp0venv311\Scripts\pythonw.exe" set "PYW=%~dp0venv311\Scripts\pythonw.exe"
if exist "%~dp0.venv\Scripts\pythonw.exe" set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%~dp0..\.venv\Scripts\pythonw.exe" set "PYW=%~dp0..\.venv\Scripts\pythonw.exe"

if /I not "%DASHBOARD_USE_FROZEN%"=="1" if /I not "%DASHBOARD_USE_FROZEN%"=="true" (
    if exist "dashboard_app.py" (
        if exist "%~dp0scripts\run_hidden.vbs" (
            wscript //nologo "%~dp0scripts\run_hidden.vbs" "\"%PYW%\" \"%~dp0dashboard_app.py\""
            exit /b 0
        )
        start "" "%PYW%" "%~dp0dashboard_app.py"
        exit /b 0
    )
)

if exist "dist\PythonTradingMonitor\PythonTradingMonitor.exe" (
    if exist "%~dp0scripts\run_hidden.vbs" (
        wscript //nologo "%~dp0scripts\run_hidden.vbs" "\"%~dp0dist\PythonTradingMonitor\PythonTradingMonitor.exe\""
        exit /b 0
    )
    start "" "dist\PythonTradingMonitor\PythonTradingMonitor.exe"
    exit /b 0
)

if exist "dashboard_app.py" (
    if exist "%~dp0scripts\run_hidden.vbs" (
        wscript //nologo "%~dp0scripts\run_hidden.vbs" "\"%PYW%\" \"%~dp0dashboard_app.py\""
        exit /b 0
    )
    start "" "%PYW%" "%~dp0dashboard_app.py"
    exit /b 0
)

echo [ERROR] No monitor found. Ensure dashboard_app.py exists.
pause
exit /b 1
