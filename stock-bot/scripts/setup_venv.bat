@echo off
setlocal EnableExtensions

REM Resolve paths relative to this script (stock-bot\scripts) and repo root.
set "SCRIPTS_DIR=%~dp0"
set "STOCK_BOT_DIR=%SCRIPTS_DIR%.."
for %%I in ("%STOCK_BOT_DIR%") do set "STOCK_BOT_DIR=%%~fI"
for %%I in ("%STOCK_BOT_DIR%\..") do set "REPO_ROOT=%%~fI"
set "VENV_DIR=%REPO_ROOT%\venv311"
set "REQS=%STOCK_BOT_DIR%\requirements.txt"
set "ACTIVATE_HELPER=%SCRIPTS_DIR%activate_venv.bat"

echo === Python Trading venv setup (3.11) ===
echo.

echo Checking for Python 3.11...
py -3.11 --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: Python 3.11 was not found via "py -3.11".
    echo.
    echo Install Python 3.11 from:
    echo   https://www.python.org/downloads/release/python-3119/
    echo   or https://www.python.org/downloads/
    echo.
    echo During install, enable "Add python.exe to PATH" and the py launcher.
    echo After installing, open a new terminal and re-run:
    echo   scripts\setup_venv.bat
    echo.
    exit /b 1
)

py -3.11 --version
echo.

if not exist "%REQS%" (
    echo ERROR: requirements.txt not found at:
    echo   %REQS%
    exit /b 1
)

if exist "%VENV_DIR%\Scripts\python.exe" (
    echo Venv already exists at:
    echo   %VENV_DIR%
    echo Reusing existing venv.
) else (
    echo Creating venv at:
    echo   %VENV_DIR%
    py -3.11 -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo ERROR: Failed to create venv.
        exit /b 1
    )
    echo Venv created.
)
echo.

echo Activating venv for pip install only...
call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 (
    echo ERROR: Failed to activate venv.
    exit /b 1
)

echo Upgrading pip...
python -m pip install --upgrade pip
if errorlevel 1 (
    echo WARNING: pip upgrade failed; continuing with existing pip.
)

echo Installing dependencies from:
echo   %REQS%
python -m pip install -r "%REQS%"
if errorlevel 1 (
    echo.
    echo ERROR: pip install failed.
    exit /b 1
)
echo.

echo Writing helper: %ACTIVATE_HELPER%
(
echo @echo off
echo call C:\Users\Owner\PythonTrading\venv311\Scripts\activate.bat
echo cd C:\Users\Owner\PythonTrading\stock-bot
echo echo Python environment ready.
echo python --version
) > "%ACTIVATE_HELPER%"
if errorlevel 1 (
    echo ERROR: Failed to write activate_venv.bat
    exit /b 1
)

echo.
echo Setup complete. Before running the bot:
echo 1. Run: scripts\activate_venv.bat
echo 2. Then: python scripts/owner_reset.py
echo.
exit /b 0
