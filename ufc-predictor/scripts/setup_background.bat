@echo off
REM Register Windows Task Scheduler jobs for UFC background processing.
REM   UFC Bot Midnight  — full Next Two Cards run daily at 12:00 AM
REM   UFC Bot Startup   — auto run on user logon (full if stale, else odds-only)

setlocal EnableDelayedExpansion
cd /d "%~dp0\.."
set "ROOT=%CD%"

echo.
echo === UFC Background Runner — Task Scheduler setup ===
echo   Project root: %ROOT%
echo.

if not exist "%ROOT%\src\background_runner.py" (
    echo [FAIL] src\background_runner.py not found.
    exit /b 1
)

if not exist "%ROOT%\models\ensemble_winner.joblib" (
    if not exist "%ROOT%\models\lgbm_winner.joblib" (
        echo [WARN] No trained model in models\ — background runs will fail until you train.
    )
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

echo Creating task: UFC Bot Midnight (daily 12:00 AM)...
schtasks /Create /TN "UFC Bot Midnight" /TR "%MIDNIGHT_CMD%" /SC DAILY /ST 00:00 /RL LIMITED /F
if errorlevel 1 (
    echo [FAIL] Could not create UFC Bot Midnight task.
    exit /b 1
)

echo Creating task: UFC Bot Startup (on user logon)...
schtasks /Create /TN "UFC Bot Startup" /TR "%STARTUP_CMD%" /SC ONLOGON /RL LIMITED /IT /F
if errorlevel 1 (
    echo [WARN] ONLOGON task denied — installing Startup folder shortcut instead...
    set "STARTUP_FOLDER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
    set "LINK_BAT=!STARTUP_FOLDER!\UFC-Bot-Background.bat"
    if not exist "!STARTUP_FOLDER!" mkdir "!STARTUP_FOLDER!"
    > "!LINK_BAT!" echo @echo off
    >> "!LINK_BAT!" echo call "%ROOT%\scripts\run_background.bat" auto startup ^>^> "%ROOT%\data\logs\background_task.log" 2^>^&1
    if exist "!LINK_BAT!" (
        echo [OK] Startup shortcut: !LINK_BAT!
    ) else (
        echo [FAIL] Could not create startup task or shortcut. Run this script as Administrator.
        exit /b 1
    )
)

echo.
echo === Setup complete ===
echo   Tasks registered for: %ROOT%
echo.
echo   Verify:
echo     schtasks /Query /TN "UFC Bot Midnight"
echo     schtasks /Query /TN "UFC Bot Startup"
echo     dir "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\UFC-Bot-Background.bat"
echo.
echo   Manual test:
echo     scripts\run_background.bat full manual
echo     scripts\run_background.bat auto startup
echo.
echo   Logs: data\logs\background_runner.log
echo.
endlocal
