@echo off
REM UFC Betting Bot — first-time setup for friends (clone from GitHub)
REM Installs ufc-predictor + betting bot, bootstraps model, opens dashboard.

cd /d "%~dp0"
set "BOT_DIR=%~dp0"
set "REPO=%BOT_DIR%.."
set "PRED=%REPO%\ufc-predictor"
title UFC Betting Bot - Friend Setup

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.11+ from https://www.python.org/downloads/
    echo         Check "Add python.exe to PATH" during install.
    pause
    exit /b 1
)

if not exist "%PRED%\config.py" (
    echo [ERROR] ufc-predictor not found at %PRED%
    echo         Clone the full repo: git clone https://github.com/dawimberly/trading-bot.git
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat
set "PYTHONPATH=%REPO%"

echo Installing dependencies...
pip install -r requirements.txt -q
pip install -r "%PRED%\requirements.txt" -q

if not exist ".env" (
    copy /Y .env.example .env >nul
    echo Created .env from .env.example — add THE_ODDS_API_KEY for live odds (optional).
)

if not exist "%PRED%\models\ensemble_winner.joblib" (
    echo.
    echo ========================================
    echo   First run: download data + train model
    echo   This may take 15-30 minutes.
    echo ========================================
    echo.
    cd /d "%PRED%"
    python main.py --refresh-data --train
    if errorlevel 1 (
        echo [ERROR] Model bootstrap failed. Check network and try again.
        pause
        exit /b 1
    )
    cd /d "%BOT_DIR%"
)

echo.
echo ========================================
echo   UFC Betting Bot is ready.
echo ========================================
echo.
echo   Dashboard: http://localhost:8502
echo   Optional: set THE_ODDS_API_KEY in ufc_betting_bot\.env
echo.

start "" http://localhost:8502
streamlit run dashboard\app.py --server.port 8502

pause
