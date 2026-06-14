@echo off
REM Build standalone UFC Predictor Dashboard (Windows GUI)
REM   build_dashboard.bat              -> windowed EXE (no console)
REM   build_dashboard.bat --debug-build -> console EXE for troubleshooting

setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "CONSOLE_FLAG=0"
if /I "%~1"=="--debug-build" set "CONSOLE_FLAG=1"

echo.
echo === UFC Dashboard - PyInstaller build ===
echo   Project root: %CD%
if "%CONSOLE_FLAG%"=="1" (
    echo   Mode: DEBUG ^(console visible^)
    set UFC_DASHBOARD_CONSOLE=1
) else (
    echo   Mode: RELEASE ^(windowed^)
    set UFC_DASHBOARD_CONSOLE=0
)
echo.

python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
)

python -c "import customtkinter, xgboost, lightgbm" 2>nul
if errorlevel 1 (
    echo Installing GUI/ML deps...
    pip install customtkinter xgboost lightgbm matplotlib
)

if not exist "ufc_betting_bot\modules\edge.py" (
    echo [FAIL] ufc_betting_bot package missing.
    exit /b 1
)

REM Report XGBoost lib path (bundled via ufc-dashboard.spec collect_dynamic_libs)
python -c "import xgboost; from pathlib import Path; p=Path(xgboost.__file__).parent/'lib'; print('XGBoost lib:', p, 'exists='+str(p.is_dir()))"

echo Building ufc-dashboard.exe via spec...
python -m PyInstaller --noconfirm --clean ufc-dashboard.spec

if errorlevel 1 (
    echo [FAIL] PyInstaller build failed.
    exit /b 1
)

call scripts\copy_runtime_assets.bat

echo.
echo === Build complete ===
echo   dist\ufc-dashboard.exe
if "%CONSOLE_FLAG%"=="1" (
    echo   ^(debug build — console visible; first launch unpacks ~280 MB, may take ~1 min^)
) else (
    echo   dist\ufc-dashboard.exe --debug  ^(allocates console at runtime^)
)
echo.
endlocal
