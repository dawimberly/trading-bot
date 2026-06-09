@echo off
REM Rebuild PythonTradingMonitor.exe — closes a running monitor first.
REM PowerShell: .\build_dashboard.bat
cd /d "%~dp0"

echo Stopping PythonTradingMonitor if running...
taskkill /IM PythonTradingMonitor.exe /F >nul 2>&1
timeout /t 2 /nobreak >nul

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt pyinstaller
    pause
    exit /b 1
)

echo Building...
.venv\Scripts\python.exe -m PyInstaller dashboard.spec --noconfirm
if errorlevel 1 (
    echo Build failed. Close the monitor and any tray icon, then try again.
    pause
    exit /b 1
)

echo.
echo Done: dist\PythonTradingMonitor\PythonTradingMonitor.exe
echo Run from THIS folder (project root), not only from dist — needs .venv and run_all.py nearby.
pause
