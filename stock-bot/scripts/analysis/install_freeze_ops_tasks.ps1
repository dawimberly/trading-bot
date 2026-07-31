# Install Windows Scheduled Tasks for freeze ops (daily hygiene + weekly confirm/deny).
#
# Usage (from stock-bot root, PowerShell):
#   powershell -ExecutionPolicy Bypass -File scripts\analysis\install_freeze_ops_tasks.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\analysis\install_freeze_ops_tasks.ps1 -DailyAt "16:30" -WeeklyAt "09:15"
#   powershell -ExecutionPolicy Bypass -File scripts\analysis\install_freeze_ops_tasks.ps1 -Uninstall
#
# Reports open via OS .md association (PyCharm if configured). Telegram/email if configured.

param(
    [string]$DailyAt = "16:30",
    [string]$WeeklyAt = "09:15",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$DailyName = "PythonTradingFreezeDailyHygiene"
$WeeklyName = "PythonTradingFreezeWeeklyPlan"
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
$DailyScript = Join-Path $Root "scripts\analysis\freeze_daily_hygiene_memo.py"
$WeeklyScript = Join-Path $Root "scripts\analysis\freeze_weekly_confirm_deny.py"
foreach ($s in @($DailyScript, $WeeklyScript)) {
    if (-not (Test-Path $s)) { throw "Missing $s" }
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $DailyName -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $WeeklyName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed tasks '$DailyName' and '$WeeklyName' (if they existed)."
    exit 0
}

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

# Daily Mon-Fri after regular hours (local)
$DailyAction = New-ScheduledTaskAction -Execute $Python -Argument "`"$DailyScript`" --open" -WorkingDirectory $Root
$DailyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $DailyAt
Register-ScheduledTask `
    -TaskName $DailyName `
    -Action $DailyAction `
    -Trigger $DailyTrigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Freeze daily hygiene memo: cleanup candidates + Telegram; never auto-applies .env." `
    -Force | Out-Null

# Saturday weekly confirm/deny (after weekly_review default 09:00)
$WeeklyAction = New-ScheduledTaskAction -Execute $Python -Argument "`"$WeeklyScript`" --open" -WorkingDirectory $Root
$WeeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At $WeeklyAt
Register-ScheduledTask `
    -TaskName $WeeklyName `
    -Action $WeeklyAction `
    -Trigger $WeeklyTrigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Freeze weekly confirm/deny plan: human gate; never auto-applies .env." `
    -Force | Out-Null

Write-Host ""
Write-Host "Installed:"
Write-Host "  $DailyName  — Mon-Fri at $DailyAt  -> $DailyScript --open"
Write-Host "  $WeeklyName — Saturday at $WeeklyAt -> $WeeklyScript --open"
Write-Host "  Python: $Python"
Write-Host "  Workdir: $Root"
Write-Host ""
Write-Host "Set in .env (recommended):"
Write-Host "  FREEZE_OPS_ENABLED=true"
Write-Host "  FREEZE_DAILY_HYGIENE_ENABLED=true"
Write-Host "  FREEZE_WEEKLY_PLAN_ENABLED=true"
Write-Host "  FREEZE_DAILY_OPEN=true"
Write-Host "  FREEZE_WEEKLY_OPEN=true"
Write-Host "  FREEZE_OPS_TELEGRAM=true"
Write-Host "  FREEZE_OPS_OLLAMA=false"
Write-Host ""
Write-Host "Test now:"
Write-Host "  & `"$Python`" `"$DailyScript`" --test --open"
Write-Host "  & `"$Python`" `"$WeeklyScript`" --test --open"
Write-Host ""
