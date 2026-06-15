@echo off
REM Rebuild PythonTradingMonitor.exe — closes a running monitor first.
REM PowerShell: .\build_dashboard.bat
cd /d "%~dp0"

echo Stopping PythonTradingMonitor if running...
taskkill /IM PythonTradingMonitor.exe /F >nul 2>&1
timeout /t 2 /nobreak >nul

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else if exist "..\.venv\Scripts\python.exe" (
    set "PY=..\.venv\Scripts\python.exe"
) else (
    echo ERROR: .venv not found. Run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt pyinstaller
    pause
    exit /b 1
)

echo Building...
"%PY%" -m PyInstaller dashboard.spec --noconfirm
if errorlevel 1 (
    echo Build failed. Close the monitor and any tray icon, then try again.
    pause
    exit /b 1
)

echo.
echo Done: dist\PythonTradingMonitor\PythonTradingMonitor.exe
echo Run from THIS folder (project root), not only from dist — needs .venv and run_all.py nearby.
pause
