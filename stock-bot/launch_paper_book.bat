@echo off
cd /d "%~dp0"
set PYTHONTRADING_ROOT=%CD%
set DASHBOARD_USE_FROZEN=false

set "PYW="
if exist "%~dp0..\venv311\Scripts\pythonw.exe" set "PYW=%~dp0..\venv311\Scripts\pythonw.exe"
if not defined PYW if exist "%~dp0venv311\Scripts\pythonw.exe" set "PYW=%~dp0venv311\Scripts\pythonw.exe"
if not defined PYW if exist "%~dp0.venv\Scripts\pythonw.exe" set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not defined PYW if exist "%~dp0..\.venv\Scripts\pythonw.exe" set "PYW=%~dp0..\.venv\Scripts\pythonw.exe"
if not defined PYW set "PYW=pythonw"

if exist "%~dp0scripts\run_hidden.vbs" (
    wscript //nologo "%~dp0scripts\run_hidden.vbs" "\"%PYW%\" \"%~dp0dashboard_app.py\" --book alpaca_paper_v2"
    exit /b 0
)

start "" "%PYW%" "%~dp0dashboard_app.py" --book alpaca_paper_v2
exit /b 0
