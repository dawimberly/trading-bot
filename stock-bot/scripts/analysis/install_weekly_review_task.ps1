# Install a Windows Scheduled Task that runs the paper weekly review every Saturday
# and opens the markdown report when finished.
#
# Usage (from stock-bot root, PowerShell):
#   powershell -ExecutionPolicy Bypass -File scripts\analysis\install_weekly_review_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\analysis\install_weekly_review_task.ps1 -At "09:00"
#   powershell -ExecutionPolicy Bypass -File scripts\analysis\install_weekly_review_task.ps1 -Uninstall
#
# The PC must be on (or wake) at the scheduled time. Report is also emailed/Telegram'd.

param(
    [string]$At = "09:00",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "PythonTradingWeeklyReview"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path (Join-Path $Root "backtester.py"))) {
    $Root = Split-Path -Parent $PSScriptRoot
}
if (-not (Test-Path (Join-Path $Root "backtester.py"))) {
    throw "Could not locate stock-bot root (expected backtester.py)."
}

$PythonCandidates = @(
    (Join-Path (Split-Path -Parent $Root) ".venv\Scripts\python.exe"),
    (Join-Path $Root ".venv\Scripts\python.exe"),
    (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
) | Where-Object { $_ -and (Test-Path $_) }

if (-not $PythonCandidates) {
    throw "No python.exe found. Activate/create the repo .venv first."
}
$Python = $PythonCandidates[0]
$Script = Join-Path $Root "scripts\analysis\weekly_review.py"
if (-not (Test-Path $Script)) {
    throw "Missing $Script"
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName' (if it existed)."
    exit 0
}

$Arg = "`"$Script`" --open"
$Action = New-ScheduledTaskAction -Execute $Python -Argument $Arg -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At $At
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "PythonTrading paper weekly review: hypothesis + A/B backtest + open report (owner approval only; never auto-applies .env)." `
    -Force | Out-Null

Write-Host ""
Write-Host "Installed scheduled task: $TaskName"
Write-Host "  When:    Every Saturday at $At (local time)"
Write-Host "  Python:  $Python"
Write-Host "  Script:  $Script --open"
Write-Host "  Workdir: $Root"
Write-Host "  Report:  $Root\data\weekly_review_latest.md"
Write-Host ""
Write-Host "Also set in .env (if not already):"
Write-Host "  WEEKLY_REVIEW_ENABLED=true"
Write-Host "  WEEKLY_REVIEW_OPEN=true"
Write-Host "  # optional pin: PYCHARM_EXE=C:\Program Files\JetBrains\PyCharm ...\bin\pycharm64.exe"
Write-Host ""
Write-Host "Open uses PyCharm directly (modules/open_markdown.py), not fragile file-assoc."
Write-Host ""
Write-Host "Test now (skip long backtests):"
Write-Host "  & `"$Python`" `"$Script`" --skip-backtest --open"
Write-Host ""
Write-Host "View / manage task:"
Write-Host "  taskschd.msc  ->  Task Scheduler Library  ->  $TaskName"
