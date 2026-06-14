@echo off
REM Build standalone ufc-predict.exe (Windows onefile console)
REM Run from UFC-Predictor project root. Requires: pip install pyinstaller rich

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo === UFC Predict - PyInstaller build ===
echo   Project root: %CD%
echo.

python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
)

python -c "import rich" 2>nul
if errorlevel 1 (
    echo Installing Rich...
    pip install rich
)

if not exist "ufc_betting_bot\modules\edge.py" (
    echo [FAIL] ufc_betting_bot package missing. Run scripts\migrate_standalone.ps1 or copy ufc_betting_bot\ here.
    exit /b 1
)

if not exist "models\ensemble_winner.joblib" (
    if not exist "models\lgbm_winner.joblib" (
        echo [WARN] No model in models\ - copy ensemble_winner.joblib before distributing EXE.
    )
)

if not exist "data\raw\fights.csv" (
    echo [WARN] data\raw\fights.csv missing - run main.py --refresh-data first or ship data\ with EXE.
)

echo Building ufc-predict.exe...
python -m PyInstaller --noconfirm --clean ^
  --onefile ^
  --console ^
  --name ufc-predict ^
  --paths "%CD%" ^
  --hidden-import=lightgbm ^
  --hidden-import=xgboost ^
  --hidden-import=sklearn.utils._typedefs ^
  --hidden-import=sklearn.neighbors._partition_nodes ^
  --hidden-import=shap ^
  --hidden-import=rich ^
  --hidden-import=rich.console ^
  --hidden-import=rich.table ^
  --hidden-import=rich.panel ^
  --hidden-import=dotenv ^
  --hidden-import=joblib ^
  --collect-submodules=lightgbm ^
  --collect-submodules=shap ^
  --collect-submodules=ufc_betting_bot ^
  src\cli_entry.py

if errorlevel 1 (
    echo.
    echo [FAIL] PyInstaller build failed.
    exit /b 1
)

call scripts\copy_runtime_assets.bat

echo.
echo === Build complete ===
echo   dist\ufc-predict.exe
echo   dist\data\            fight data + cache
echo   dist\models\          trained model
echo   dist\ufc_betting_bot\ .env (API keys)
echo.
echo Usage:
echo   dist\ufc-predict.exe "Freedom 250"
echo   dist\ufc-predict.exe --watch --auto-odds
echo.
endlocal
