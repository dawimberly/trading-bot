@echo off
REM Autonomous overnight paper bot startup + 9:00 AM ET Telegram summary.
REM Double-click before bed — runs silently via pythonw (see logs\autostart_paper.log).
setlocal
cd /d "%~dp0"

if not exist "logs" mkdir "logs"

set "PYW="
if exist ".venv\Scripts\pythonw.exe" set "PYW=.venv\Scripts\pythonw.exe"
if not defined PYW if exist "..\.venv\Scripts\pythonw.exe" set "PYW=..\.venv\Scripts\pythonw.exe"
if not defined PYW set "PYW=pythonw"

start "" /B "%PYW%" -u "scripts\autostart_paper_bot.py" >> "logs\autostart_paper.log" 2>&1
exit /b 0
