# Install Windows Scheduled Task for nightly Alpaca universe -> watchlist scan.
#
# Usage (from stock-bot root, PowerShell):
#   powershell -ExecutionPolicy Bypass -File scripts\ops\install_nightly_universe_scan_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\ops\install_nightly_universe_scan_task.ps1 -At "20:00"
#   powershell -ExecutionPolicy Bypass -File scripts\ops\install_nightly_universe_scan_task.ps1 -Uninstall
#
# Default 8:00 PM local time (CT on this machine) — after cash equity close / bar settle.
# Research watchlist under data\watchlists\ — merged into screener when
# MERGE_NIGHTLY_WATCHLIST=true (paper_v2). Still does not place orders itself.

param(
    [string]$At = "20:00",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "PythonTradingNightlyUniverseScan"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path (Join-Path $Root "backtester.py"))) {
    $Root = Split-Path -Parent $PSScriptRoot
}
if (-not (Test-Path (Join-Path $Root "backtester.py"))) {
    throw "Could not locate stock-bot root (expected backtester.py)."
}

$PythonCandidates = @(
    (Join-Path (Split-Path -Parent $Root) "venv311\Scripts\python.exe"),
    (Join-Path (Split-Path -Parent $Root) ".venv\Scripts\python.exe"),
    (Join-Path $Root ".venv\Scripts\python.exe"),
    (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
) | Where-Object { $_ -and (Test-Path $_) }

if (-not $PythonCandidates) {
    throw "No python.exe found. Activate/create the repo venv first."
}
$Python = $PythonCandidates[0]
$Script = Join-Path $Root "scripts\ops\nightly_universe_scan.py"
if (-not (Test-Path $Script)) {
    throw "Missing script: $Script"
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed task: $TaskName"
    exit 0
}

$Arg = "`"$Script`""
$Action = New-ScheduledTaskAction -Execute $Python -Argument $Arg -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Daily -At $At
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null

Write-Host "Installed: $TaskName"
Write-Host "  Python:  $Python"
Write-Host "  Script:  $Script"
Write-Host "  DailyAt: $At (local time)"
Write-Host "  Output:  $Root\data\watchlists\"
Write-Host "Freeze-safe: writes watchlist JSON; paper merges it when MERGE_NIGHTLY_WATCHLIST=true (hold count still capped by MAX_ACTIVE_TICKERS)."
