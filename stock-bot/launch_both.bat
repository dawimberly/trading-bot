@echo off
REM Start live + paper Sharpe chase bots (two processes, isolated data).
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Run: python -m venv .venv ^& pip install -r requirements.txt
    pause
    exit /b 1
)

.venv\Scripts\python.exe launch_bots.py %*
if errorlevel 1 pause
