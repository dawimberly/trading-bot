set PAPER_DEPLOY_DEBUG=false
set PYTHONUNBUFFERED=1
set MARKOV_HMM_ENABLED=true
set MARKOV_HMM_PRIMARY_REGIME=false
set GARCH_VOL_ENABLED=true
set PAPER_DYNAMIC_VTI=true
set DAILY_BANK_ENABLED=true
set HISTORICAL_NEWS_ENABLED=true
cd /d c:\Users\Owner\PythonTrading\stock-bot
"c:\Users\Owner\PythonTrading\.venv\Scripts\python.exe" -u backtester.py --days 1000 --paper-aggressive --no-thinking > "c:\Users\Owner\PythonTrading\stock-bot\backtest_v154_thorough_1000.txt" 2> "c:\Users\Owner\PythonTrading\stock-bot\backtest_v154_thorough_1000.err.txt"
