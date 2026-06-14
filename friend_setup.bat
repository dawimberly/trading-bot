@echo off
REM Forward to stock-bot (monorepo layout)
cd /d "%~dp0stock-bot"
call "%~dp0stock-bot\friend_setup.bat"
