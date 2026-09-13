@echo off
REM Monday pre-market checklist — verify both locks, reset, heartbeats, Telegram, health.
REM Prefer repo-root .venv (full deps). stock-bot\.venv may be incomplete.
setlocal
cd /d "%~dp0"
set "STOCK_BOT=%~dp0"
if "%STOCK_BOT:~-1%"=="\" set "STOCK_BOT=%STOCK_BOT:~0,-1%"
for %%I in ("%STOCK_BOT%\..") do set "REPO_ROOT=%%~fI"
set PYTHONTRADING_ROOT=%STOCK_BOT%
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if not exist "logs" mkdir "logs"

set "PY="
if exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
    set "PY=%REPO_ROOT%\.venv\Scripts\python.exe"
) else if exist "%STOCK_BOT%\.venv\Scripts\python.exe" (
    set "PY=%STOCK_BOT%\.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo ============================================================
echo   PythonTrading — Monday Checklist
echo   Using: %PY%
echo ============================================================
echo.

"%PY%" -u "%STOCK_BOT%\scripts\monday_checklist.py" %*
set EXITCODE=%ERRORLEVEL%

echo.
if %EXITCODE% NEQ 0 (
    echo [RESULT] Monday checklist reported FAIL — exit code %EXITCODE%
) else (
    echo [RESULT] Monday checklist complete — exit code 0
)
echo.
pause
exit /b %EXITCODE%
