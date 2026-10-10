$ErrorActionPreference = "Continue"
$log = "c:\Users\Owner\PythonTrading\stock-bot\scripts\analysis\_smart_stops_365.log"
$dailyPid = 12784
Write-Host "Waiting for daily-bank PID $dailyPid ..."
Wait-Process -Id $dailyPid -ErrorAction SilentlyContinue
# Also wait until no compare-daily-bank python remains
for ($i=0; $i -lt 60; $i++) {
  $left = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'compare-daily-bank' })
  if ($left.Count -eq 0) { break }
  Start-Sleep -Seconds 10
}
Write-Host "Daily-bank cleared at $(Get-Date -Format o); starting smart-stops A/B"
Set-Location "c:\Users\Owner\PythonTrading\stock-bot"
$env:PAPER_DEPLOY_DEBUG = "false"
$env:MARKOV_HMM_ENABLED = "false"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
& "C:\Users\Owner\PythonTrading\.venv\Scripts\python.exe" -u backtester.py --days 365 --paper-aggressive --compare-smart-stops --no-thinking 2>&1 | Tee-Object -FilePath $log
Write-Host "SMART_STOPS_EXIT=$LASTEXITCODE at $(Get-Date -Format o)"
