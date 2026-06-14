@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
"%USERPROFILE%\.grok\bin\grok.exe" %*
