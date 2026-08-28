@echo off
setlocal EnableExtensions
REM Daily ANALYZE: write docs\DAILY_ANALYZE_*.md then open LAST in PyCharm.
REM Task Scheduler (owner must register): weekdays 15:15 America/Chicago
REM   Program: this bat   Start in: stock-bot
REM Do not register unless owner says. No Telegram.

set "STOCK_BOT=%~dp0..\.."
for %%I in ("%STOCK_BOT%") do set "STOCK_BOT=%%~fI"
cd /d "%STOCK_BOT%"

set "PY="
if exist "%STOCK_BOT%\..\venv311\Scripts\python.exe" (
    set "PY=%STOCK_BOT%\..\venv311\Scripts\python.exe"
) else if exist "%STOCK_BOT%\venv311\Scripts\python.exe" (
    set "PY=%STOCK_BOT%\venv311\Scripts\python.exe"
) else if exist "%STOCK_BOT%\..\.venv\Scripts\python.exe" (
    set "PY=%STOCK_BOT%\..\.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

"%PY%" -u "%STOCK_BOT%\scripts\analysis\daily_analyze.py"
set "RC=%ERRORLEVEL%"

set "LAST=%STOCK_BOT%\docs\DAILY_ANALYZE_LAST.md"
if not exist "%LAST%" (
    echo [daily_analyze] Report missing — skip PyCharm.
    exit /b %RC%
)

set "PC="
if defined PYCHARM_EXE if exist "%PYCHARM_EXE%" set "PC=%PYCHARM_EXE%"

REM PATH / where
if not defined PC (
    for /f "delims=" %%P in ('where pycharm64.exe 2^>nul') do (
        if exist "%%P" set "PC=%%P"
    )
)

REM Common user installs
if not defined PC if exist "%LOCALAPPDATA%\Programs\PyCharm Community Edition\bin\pycharm64.exe" (
    set "PC=%LOCALAPPDATA%\Programs\PyCharm Community Edition\bin\pycharm64.exe"
)
if not defined PC if exist "%LOCALAPPDATA%\Programs\PyCharm\bin\pycharm64.exe" (
    set "PC=%LOCALAPPDATA%\Programs\PyCharm\bin\pycharm64.exe"
)

REM Program Files JetBrains (this machine: PyCharm 2026.1.2)
if not defined PC if exist "C:\Program Files\JetBrains\PyCharm 2026.1.2\bin\pycharm64.exe" (
    set "PC=C:\Program Files\JetBrains\PyCharm 2026.1.2\bin\pycharm64.exe"
)
if not defined PC (
    for /d %%D in ("C:\Program Files\JetBrains\PyCharm*") do (
        if exist "%%D\bin\pycharm64.exe" set "PC=%%D\bin\pycharm64.exe"
    )
)
if not defined PC if exist "C:\Program Files\JetBrains\PyCharm Community Edition\bin\pycharm64.exe" (
    set "PC=C:\Program Files\JetBrains\PyCharm Community Edition\bin\pycharm64.exe"
)

REM JetBrains Toolbox
if not defined PC (
    for /d %%A in ("%LOCALAPPDATA%\JetBrains\Toolbox\apps\PyCharm*") do (
        for /d %%C in ("%%A\*") do (
            for /d %%V in ("%%C\*") do (
                if exist "%%V\bin\pycharm64.exe" set "PC=%%V\bin\pycharm64.exe"
            )
        )
    )
)

if defined PC (
    echo [daily_analyze] Opening with: %PC%
    start "" "%PC%" "%LAST%"
) else (
    echo [daily_analyze] pycharm64.exe not found — opening LAST.md with default app.
    start "" "%LAST%"
)

exit /b %RC%
