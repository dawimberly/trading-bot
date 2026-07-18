@echo off
REM One-click: cancel backtests + full verify + official v1.5 lock.
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Realistic Research v1.5 - Lock and Verify
echo ============================================================
echo.

python scripts\lock_v15.py %*
set LOCK_RC=%errorlevel%

echo.
if %LOCK_RC%==0 (
    echo >>> v1.5 Locked and Ready for Monday ^<
    echo Next: python scripts\owner_reset.py
) else (
    echo [WARN] lock_v15.py returned %LOCK_RC% - review output above
)

echo ============================================================
pause
exit /b %LOCK_RC%
