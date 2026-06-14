@echo off
REM Trading bot — runs on Windows logon (installed by scripts\setup_background.bat)
cd /d "%~dp0\.."
call scripts\run_background.bat auto startup
