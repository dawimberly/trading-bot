@echo off
cd /d "%~dp0"
python scripts\account\preflight_spy.py
if errorlevel 1 exit /b 1
python run_spy.py
