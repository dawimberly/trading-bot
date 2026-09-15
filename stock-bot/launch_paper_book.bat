@echo off
cd /d "%~dp0"
set PYTHONTRADING_ROOT=%CD%
REM Full Stock-bot chrome forced to Medium SoT (cyan scrolling tape).
REM Do NOT use --paper-book here — that strips the header/tape the owner wants.
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

echo [INFO] Paper SoT (Medium / alpaca_paper_v2) — full chrome + cyan tape
start "" "%PYW%" "%~dp0dashboard_app.py" --book alpaca_paper_v2
exit /b 0
