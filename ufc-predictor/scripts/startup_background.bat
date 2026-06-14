@echo off
REM UFC Predictor — runs on Windows logon (installed by scripts\setup_background.bat)
cd /d "%~dp0\.."
call scripts\run_background.bat auto startup >> data\logs\background_task.log 2>&1
