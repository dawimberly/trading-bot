@echo off
REM Overnight / morning research pack — paper-focused, read-only.
REM Schedule via Task Scheduler daily 08:00 local (scripts/setup_research_pack_task.py).
REM Do NOT wire into run_paper_bot.py until the script is stable.
REM
REM Brief Telegram (Section 1 + recommendation only) when:
REM   OVERNIGHT_PACK_ENABLED=true
REM Full report: reports\research\YYYY-MM-DD.md (open in PyCharm)
REM Optional: OPEN_RESEARCH_PACK_IN_EDITOR=true
setlocal
cd /d "%~dp0"
set "STOCK_BOT=%~dp0"
if "%STOCK_BOT:~-1%"=="\" set "STOCK_BOT=%STOCK_BOT:~0,-1%"
for %%I in ("%STOCK_BOT%\..") do set "REPO_ROOT=%%~fI"
set PYTHONTRADING_ROOT=%STOCK_BOT%
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if not exist "logs" mkdir "logs"
if not exist "data" mkdir "data"
if not exist "reports\research" mkdir "reports\research"

set "PY="
if exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
    set "PY=%REPO_ROOT%\.venv\Scripts\python.exe"
) else if exist "%REPO_ROOT%\venv311\Scripts\python.exe" (
    set "PY=%REPO_ROOT%\venv311\Scripts\python.exe"
) else if exist "%STOCK_BOT%\.venv\Scripts\python.exe" (
    set "PY=%STOCK_BOT%\.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo ============================================================
echo   PythonTrading — Overnight Research Pack
echo   Using: %PY%
echo ============================================================
echo.

"%PY%" -u "%STOCK_BOT%\scripts\ops\overnight_research_pack.py" %*
set EXITCODE=%ERRORLEVEL%

echo.
if %EXITCODE% NEQ 0 (
    echo [RESULT] Research pack FAILED — exit code %EXITCODE%
) else (
    echo [RESULT] Research pack complete — exit code 0
)
echo.
if /I "%~1"=="--pause" pause
exit /b %EXITCODE%
