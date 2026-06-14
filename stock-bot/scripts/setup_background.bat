@echo off
REM Register Windows Task Scheduler jobs for PythonTrading background health checks.
REM   Trading Bot - Midnight  — full preflight, status, data refresh, paper cycle
REM   Trading Bot - Startup   — quick status, safety, ensure run_paper_bot if needed

setlocal EnableDelayedExpansion
cd /d "%~dp0\.."
set "ROOT=%CD%"

echo.
echo === PythonTrading Background Runner — Task Scheduler setup ===
echo   Project root: %ROOT%
echo.

if not exist "%ROOT%\scripts\background_runner.py" (
    echo [FAIL] scripts\background_runner.py not found.
    exit /b 1
)

if not exist "%ROOT%\run_paper_bot.py" (
    echo [WARN] run_paper_bot.py not found — paper supervisor auto-start will fail.
)

set "RUNNER=%ROOT%\scripts\run_background.bat"
if not exist "%RUNNER%" (
    echo [FAIL] scripts\run_background.bat missing.
    exit /b 1
)

where schtasks >nul 2>&1
if errorlevel 1 (
    echo [FAIL] schtasks not found — requires Windows Task Scheduler.
    exit /b 1
)

set "MIDNIGHT_CMD=cmd /c \"%RUNNER%\" full midnight"
set "STARTUP_CMD=cmd /c \"%RUNNER%\" auto startup"

echo Creating task: Trading Bot - Midnight (daily 12:00 AM)...
schtasks /Create /TN "Trading Bot - Midnight" /TR "%MIDNIGHT_CMD%" /SC DAILY /ST 00:00 /RL LIMITED /F
if errorlevel 1 (
    echo [FAIL] Could not create Trading Bot - Midnight task.
    exit /b 1
)

echo Creating task: Trading Bot - Startup (on user logon)...
schtasks /Create /TN "Trading Bot - Startup" /TR "%STARTUP_CMD%" /SC ONLOGON /RL LIMITED /IT /F
if errorlevel 1 (
    echo [WARN] ONLOGON task denied — installing Startup folder shortcut instead...
    set "STARTUP_FOLDER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
    set "LINK_BAT=!STARTUP_FOLDER!\Trading-Bot-Background.bat"
    if not exist "!STARTUP_FOLDER!" mkdir "!STARTUP_FOLDER!"
    > "!LINK_BAT!" echo @echo off
    >> "!LINK_BAT!" echo call "%ROOT%\scripts\run_background.bat" auto startup ^>^> "%ROOT%\logs\background_task.log" 2^>^&1
    if exist "!LINK_BAT!" (
        echo [OK] Startup shortcut: !LINK_BAT!
    ) else (
        echo [FAIL] Could not create startup task or shortcut. Run as Administrator.
        exit /b 1
    )
)

echo.
echo === Setup complete ===
echo   Tasks registered for: %ROOT%
echo.
echo   Verify:
echo     schtasks /Query /TN "Trading Bot - Midnight"
echo     schtasks /Query /TN "Trading Bot - Startup"
echo.
echo   Manual test:
echo     scripts\run_background.bat full manual
echo     scripts\run_background.bat auto startup
echo.
echo   Logs:
echo     logs\background_runner.log
echo     logs\background_task.log
echo     logs\background_runner_manifest.json
echo.
echo   Env (optional):
echo     TRADING_BOT_AUTO_START_PAPER=true   start run_paper_bot.py on startup if not running
echo.
endlocal
