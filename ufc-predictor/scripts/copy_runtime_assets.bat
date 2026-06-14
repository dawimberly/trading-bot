@echo off
REM Copy data, models, cache, and .env beside dist\ EXEs.
REM Called from build_exe.bat / build_dashboard.bat after PyInstaller.

setlocal
cd /d "%~dp0\.."

if not exist "dist" mkdir "dist"
if not exist "dist\data" mkdir "dist\data"
if not exist "dist\models" mkdir "dist\models"
if not exist "dist\ufc_betting_bot" mkdir "dist\ufc_betting_bot"

echo Copying runtime assets to dist\ ...
xcopy /E /I /Y /Q "data\*" "dist\data\" >nul 2>&1
xcopy /E /I /Y /Q "models\*" "dist\models\" >nul 2>&1
if exist ".env" copy /Y ".env" "dist\.env" >nul
if exist "ufc_betting_bot\.env" copy /Y "ufc_betting_bot\.env" "dist\ufc_betting_bot\.env" >nul
if not exist "dist\ufc_betting_bot\.env" if exist "ufc_betting_bot\.env.example" (
    copy /Y "ufc_betting_bot\.env.example" "dist\ufc_betting_bot\.env.example" >nul
)

endlocal
