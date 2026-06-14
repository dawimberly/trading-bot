@echo off
REM Launcher for scheduled / manual background UFC bot runs.
REM Usage: run_background.bat [mode] [trigger]
REM   mode:    auto | full | lightweight  (default: auto)
REM   trigger: startup | midnight | manual (default: manual)

setlocal
cd /d "%~dp0\.."

set "MODE=%~1"
set "TRIGGER=%~2"
if "%MODE%"=="" set "MODE=auto"
if "%TRIGGER%"=="" set "TRIGGER=manual"

where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 src\background_runner.py --mode %MODE% --trigger %TRIGGER%
    exit /b %ERRORLEVEL%
)

python src\background_runner.py --mode %MODE% --trigger %TRIGGER%
exit /b %ERRORLEVEL%
