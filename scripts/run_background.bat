@echo off
REM Launcher for scheduled / manual Trading Bot background runs.
REM Usage: run_background.bat [mode] [trigger]
REM   mode:    auto | full | lightweight  (default: auto)
REM   trigger: startup | midnight | manual (default: manual)

setlocal
cd /d "%~dp0\.."

set "MODE=%~1"
set "TRIGGER=%~2"
if "%MODE%"=="" set "MODE=auto"
if "%TRIGGER%"=="" set "TRIGGER=manual"

set "LOG=%CD%\logs\background_task.log"
if not exist "%CD%\logs" mkdir "%CD%\logs"

where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 scripts\background_runner.py --mode %MODE% --trigger %TRIGGER% >> "%LOG%" 2>&1
    exit /b %ERRORLEVEL%
)

python scripts\background_runner.py --mode %MODE% --trigger %TRIGGER% >> "%LOG%" 2>&1
exit /b %ERRORLEVEL%
