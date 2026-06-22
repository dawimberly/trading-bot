@echo off
setlocal EnableExtensions

REM Daily driver — double-click from desktop shortcut.
REM Always resolves paths from this file's location (not your current folder).

set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"
set "STOCK_BOT=%REPO_ROOT%\stock-bot"
set "RESET_SCRIPT=%STOCK_BOT%\scripts\owner_reset.py"
set "DASHBOARD=%STOCK_BOT%\dashboard_app.py"

if not exist "%DASHBOARD%" (
    echo.
    echo [ERROR] Could not find the trading bot project.
    echo.
    echo   Expected folder: %STOCK_BOT%
    echo.
    echo   Fix: place this .bat in your PythonTrading project root, or edit the
    echo   desktop shortcut Target to point at the real copy of this file.
    echo.
    pause
    exit /b 1
)

if not exist "%RESET_SCRIPT%" (
    echo.
    echo [ERROR] Missing launcher script:
    echo   %RESET_SCRIPT%
    echo.
    pause
    exit /b 1
)

cd /d "%STOCK_BOT%"
set "PYTHONTRADING_ROOT=%STOCK_BOT%"
title PythonTrading - Starting...

set "PY=python"
if exist "%STOCK_BOT%\.venv\Scripts\python.exe" (
    set "PY=%STOCK_BOT%\.venv\Scripts\python.exe"
) else if exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
    set "PY=%REPO_ROOT%\.venv\Scripts\python.exe"
)

echo.
echo ========================================
echo   PythonTrading - Daily Start
echo ========================================
echo.
echo   This window shows startup progress only.
echo   The dashboard opens in a separate window.
echo.
echo   Project: %STOCK_BOT%
echo ========================================
echo.

"%PY%" -u "%RESET_SCRIPT%"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [ERROR] Startup failed ^(exit code %RC%^). Read the messages above.
    echo.
    pause
    exit /b %RC%
)

echo.
echo ========================================
echo   Started successfully.
echo   - Live bot  ^(conservative^)
echo   - Paper bot ^(aggressive^)
echo   - Desktop dashboard ^(pythonw^)
echo.
echo   Sign in when the dashboard opens.
echo   Wait ~60 seconds, then check Overview for fresh heartbeats.
echo ========================================
echo.
echo Minimizing this window in 8 seconds...
timeout /t 8 /nobreak >nul

powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -Name NativeMethods -Namespace Win32 -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);'; $h = (Get-Process -Id $PID).MainWindowHandle; if ($h -ne [IntPtr]::Zero) { [Win32.NativeMethods]::ShowWindow($h, 6) | Out-Null }"

endlocal
exit /b 0
