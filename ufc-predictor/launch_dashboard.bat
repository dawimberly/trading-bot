@echo off
REM Launch UFC Predictor GUI (no console) - for manual use or legacy wrappers.
cd /d "%~dp0dist"
if not exist "ufc-dashboard.exe" (
    echo [ERROR] dist\ufc-dashboard.exe not found. Run build_dashboard.bat first.
    pause
    exit /b 1
)
start "" "ufc-dashboard.exe"
