@echo off
setlocal
set OUT=c:\Users\Owner\PythonTrading\stock-bot\backtest_v154_thorough_1000.txt
set ERR=c:\Users\Owner\PythonTrading\stock-bot\backtest_v154_thorough_1000.err.txt
set PIDFILE=c:\Users\Owner\PythonTrading\stock-bot\backtest_v154_thorough_1000.pid.txt
set PY=c:\Users\Owner\PythonTrading\.venv\Scripts\python.exe
:loop
timeout /t 120 /nobreak >nul
REM still running?
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'backtester.py --days 1000' }) { exit 0 } else { exit 1 }"
if errorlevel 1 goto done
goto loop
:done
timeout /t 5 /nobreak >nul
"%PY%" -u -c "import re, pathlib, datetime; p=pathlib.Path(r'c:\\Users\\Owner\\PythonTrading\\stock-bot\\backtest_v154_thorough_1000.txt'); t=p.read_text(encoding='utf-8', errors='replace'); keys=['Total Return:','VTI Buy & Hold:','Sharpe Ratio:','Sortino Ratio:','Calmar Ratio:','Max Drawdown:','Final Equity:','Total orders:','SPY signals:','NYSE signals:','Crypto signals:','Profit factor:','Win rate (daily):','Rolling Sharpe:','Simulation:','Historical news simulation:']; rows=[]; 
for k in keys:
  m=re.search(rf'^{re.escape(k)}\s*(.+)$', t, re.M); rows.append((k.rstrip(':'), (m.group(1).strip() if m else 'n/a')))
# sleeve attribution block
sa=re.search(r'Stat Arb[^\n]*', t); shorts=re.search(r'Protective Shorts:[^\n]*', t); core=re.search(r'Final core allocator:[^\n]*', t); dyn=re.search(r'dynamic_vti:\s*[^\n]*', t); think=re.search(r'thinking_engine:\s*[^\n]*', t)
lines=['','='*72,'v1.5.4 THOROUGH 1000d SUMMARY TABLE','Generated: '+datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),'Command: python -u backtester.py --days 1000 --paper-aggressive --no-thinking','Flags: GARCH ON | Dynamic VTI ON | Daily Banking ON | RHYME primary | HMM soft (retrain every 5 bars, n_states=5, train=252d) | HMM primary OFF | thinking OFF | news/Felix per profile','='*72]
for k,v in rows: lines.append(f'{k:<28} {v}')
if core: lines.append(core.group(0))
if dyn: lines.append(dyn.group(0).strip())
if think: lines.append(think.group(0).strip())
if sa: lines.append(sa.group(0)[:200])
if shorts: lines.append(shorts.group(0)[:200])
# vs VTI
tr=re.search(r'Total Return:\s*([-\d.]+)%', t); vb=re.search(r'VTI Buy & Hold:\s*([-\d.]+)%', t)
if tr and vb:
  d=float(tr.group(1))-float(vb.group(1)); lines.append(f\"{'vs VTI (pp)':<28} {d:+.2f} pp\")
lines.append('='*72); lines.append('')
# only append if not already present
if 'v1.5.4 THOROUGH 1000d SUMMARY TABLE' not in t:
  with p.open('a', encoding='utf-8') as f: f.write('\n'.join(lines))
  print('SUMMARY APPENDED')
else:
  print('SUMMARY ALREADY PRESENT')
print('OUT BYTES', p.stat().st_size)
"
echo WATCHER_DONE >> "%OUT%.watcher.log"
