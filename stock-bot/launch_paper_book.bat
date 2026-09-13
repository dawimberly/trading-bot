@echo off
cd /d "%~dp0"
set PYTHONTRADING_ROOT=%CD%
set DASHBOARD_USE_FROZEN=false

set "PYW="
if exist "%~dp0..\venv311\Scripts\pythonw.exe" (
    set "PYW=%~dp0..\venv311\Scripts\pythonw.exe"
) else if exist "%~dp0venv311\Scripts\pythonw.exe" (
    set "PYW=%~dp0venv311\Scripts\pythonw.exe"
) else if exist "%~dp0.venv\Scripts\pythonw.exe" (
    set "PYW=%~dp0.venv\Scripts\pythonw.exe"
) else if exist "%~dp0..\.venv\Scripts\pythonw.exe" (
    set "PYW=%~dp0..\.venv\Scripts\pythonw.exe"
) else (
    set "PYW=pythonw"
)

echo [INFO] Paper book display
start "" "%PYW%" "%~dp0dashboard_app.py" --paper-book
exit /b 0
