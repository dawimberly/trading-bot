# Install Windows Scheduled Task for daily Gate A/B logger (measure-only).
#
# Usage (from stock-bot root, PowerShell):
#   powershell -ExecutionPolicy Bypass -File scripts\ops\install_gate_ab_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\ops\install_gate_ab_task.ps1 -At "15:20"
#   powershell -ExecutionPolicy Bypass -File scripts\ops\install_gate_ab_task.ps1 -Uninstall
#
# Default 3:20 PM local — after US equity close on Central Time machines.
# Writes data\gate_ab_log.csv only. Does not change .env or place orders.

param(
    [string]$At = "15:20",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "PythonTradingGateABLog"
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
$Script = Join-Path $Root "scripts\ops\log_gate_ab.py"
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
Write-Host "  Output:  $Root\data\gate_ab_log.csv"
Write-Host "Freeze-safe: CSV only; no .env / rebalance / orders."
