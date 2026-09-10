# Install nightly auto-tune Task Scheduler job (T0–T2 auto, T3+ propose).
#
#   powershell -ExecutionPolicy Bypass -File scripts\analysis\install_auto_tune_nightly_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\analysis\install_auto_tune_nightly_task.ps1 -At "21:30"
#   powershell -ExecutionPolicy Bypass -File scripts\analysis\install_auto_tune_nightly_task.ps1 -Uninstall

param(
    [string]$At = "21:15",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "PythonTradingAutoTuneNightly"
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
    throw "No python.exe found."
}
$Python = $PythonCandidates[0]
$Script = Join-Path $Root "scripts\analysis\auto_tune_eval.py"
if (-not (Test-Path $Script)) { throw "Missing $Script" }

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed task '$TaskName' (if it existed)."
    exit 0
}

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$Script`" --nightly" `
    -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Daily -At $At

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Nightly paper auto-tune: T0/T1 hygiene + T2 auto-apply (if armed) + T3+ propose. Never live/VTI%." `
    -Force | Out-Null

Write-Host "Installed '$TaskName' daily at $At"
Write-Host "  python: $Python"
Write-Host "  script: $Script --nightly"
Write-Host "Requires paper_v2 .env: AUTO_TUNE_ENABLED=true AUTO_TUNE_APPLY=true AUTO_APPLY_MAX_TIER=2"
