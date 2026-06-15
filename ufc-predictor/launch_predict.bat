@echo off
REM Launch UFC Predict CLI with sensible defaults (next two cards + odds).
cd /d "%~dp0"

set "EXE=dist\ufc-predict.exe"
if not exist "%EXE%" (
    echo [ERROR] %EXE% not found.
    echo Run build_exe.bat from this folder first.
    pause
    exit /b 1
)

"%EXE%" --next-two --odds
if errorlevel 1 pause
