@echo off
REM Build monitor EXE + trading bot EXE into stock-bot/dist/
cd /d "%~dp0"
set PYTHONTRADING_ROOT=%CD%
set BUILD_ALL=1

echo.
echo ========================================
echo   PythonTrading — full EXE build
echo   Output: stock-bot\dist\
echo ========================================
echo.

echo [1/2] PythonTradingMonitor...
call "%~dp0build_dashboard.bat"
if errorlevel 1 (
    echo [FAIL] Monitor build failed.
    exit /b 1
)

echo.
echo [2/2] Weinstein-Trading-Bot...
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else if exist "..\.venv\Scripts\python.exe" (
    set "PY=..\.venv\Scripts\python.exe"
) else (
    set "PY=python"
)
"%PY%" build_exe.py --no-clean
if errorlevel 1 (
    echo [FAIL] Bot EXE build failed.
    exit /b 1
)

echo.
echo === Build complete ===
echo   dist\PythonTradingMonitor\PythonTradingMonitor.exe
echo   dist\Weinstein-Trading-Bot.exe
echo   dist\Start Weinstein Trading Bot.bat
echo   dist\.env synced from stock-bot\.env ^(fallback only^)
echo.
echo Launch: ..\start.bat  ^(prefers frozen EXE^)  or  launch.bat
echo Config: edit stock-bot\.env ^(authoritative^)
echo.
pause
