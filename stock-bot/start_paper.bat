@echo off
REM Best Paper v2.2 — paper chase bot only (no dashboard, no duplicate run_all).
REM Double-click or run from cmd. Keep this window open while trading.

cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not exist ".venv\Scripts\python.exe" if exist "..\.venv\Scripts\python.exe" set "PY=..\.venv\Scripts\python.exe"

echo Starting paper bot (run_paper_bot.py)...
echo Heartbeat: paper_chase_heartbeat.json
echo Stop with Ctrl+C in this window.
echo.

"%PY%" -u run_paper_bot.py
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" echo Paper bot exited with code %EC%.
pause
exit /b %EC%
