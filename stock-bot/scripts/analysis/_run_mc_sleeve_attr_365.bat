@echo off
cd /d C:\Users\Owner\PythonTrading\stock-bot
set MC_SLEEVE_ATTR_RUNS=50
set PYTHONUNBUFFERED=1
set PAPER_DEPLOY_DEBUG=false
"C:\Users\Owner\PythonTrading\.venv\Scripts\python.exe" -u scripts\analysis\_run_mc_sleeve_attribution_365.py > scripts\analysis\mc_sleeve_attr_365_run.log 2> scripts\analysis\mc_sleeve_attr_365_run.err.log
echo EXIT=%ERRORLEVEL%>> scripts\analysis\mc_sleeve_attr_365_run.log
