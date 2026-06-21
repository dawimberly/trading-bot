@echo off
set "ROOT=%~dp0stock-bot"
cd /d "%ROOT%"
set PYTHONTRADING_ROOT=%ROOT%

if exist "dist\Weinstein-Trading-Bot.exe" (
    echo [INFO] Starting frozen bot: dist\Weinstein-Trading-Bot.exe
    "dist\Weinstein-Trading-Bot.exe"
    exit /b %ERRORLEVEL%
)

call launch.bat
