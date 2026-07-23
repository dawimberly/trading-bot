@echo off
REM Remove the nightly autonomous paper-bot Task Scheduler job.
setlocal
set "TASK=PythonTrading_Autonomous_Paper"

echo.
echo Removing scheduled task: %TASK%
schtasks /Delete /TN "%TASK%" /F >nul 2>&1
if errorlevel 1 (
    echo [WARN] Task not found or could not be deleted.
    echo        It may already be removed.
    exit /b 1
)

echo [OK] Task removed: %TASK%
echo.
exit /b 0
