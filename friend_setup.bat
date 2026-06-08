@echo off
REM PythonTrading — first-time setup for friends (clone from GitHub, run locally)
REM Creates .venv, installs deps, opens the portal in your browser.

cd /d "%~dp0"
title PythonTrading Friend Setup

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.11+ from https://www.python.org/downloads/
    echo         Check "Add python.exe to PATH" during install.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat
echo Installing dependencies...
pip install -r requirements.txt -q

if not exist "logs" mkdir logs

echo.
echo ========================================
echo   PythonTrading is ready.
echo ========================================
echo.
echo   1. Browser opens to the portal
echo   2. Register your account
echo   3. Enter your Alpaca PAPER API keys
echo   4. Bot tab: Download market data, then Start bot
echo.
echo   Get paper keys: https://app.alpaca.markets/paper/dashboard/overview
echo.

start "" http://localhost:8501
streamlit run portal.py

pause
