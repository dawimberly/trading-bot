@echo off
cd /d "%~dp0"
set PYTHONTRADING_ROOT=%CD%

set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"

echo [INFO] Realistic Research v1.5 — full system verify
"%PY%" scripts\full_system_verify.py
set EXITCODE=%ERRORLEVEL%
echo.
if %EXITCODE% NEQ 0 (
    echo [RESULT] Verification reported issues — exit code %EXITCODE%
) else (
    echo [RESULT] Verification complete — exit code 0
)
pause
exit /b %EXITCODE%
