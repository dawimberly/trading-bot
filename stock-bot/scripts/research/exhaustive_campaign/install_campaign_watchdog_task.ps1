# Install Windows Scheduled Task: exhaustive campaign watchdog (every 5 minutes).
#
# Keeps the multi-hour research campaign alive across agent/shell death.
# Freeze-safe: research only — never wires paper/live.
#
# Usage (PowerShell, from anywhere):
#   powershell -ExecutionPolicy Bypass -File stock-bot\scripts\research\exhaustive_campaign\install_campaign_watchdog_task.ps1
#   powershell -ExecutionPolicy Bypass -File ...\install_campaign_watchdog_task.ps1 -Uninstall
#   powershell -ExecutionPolicy Bypass -File ...\install_campaign_watchdog_task.ps1 -RunNow

param(
    [switch]$Uninstall,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$TaskName = "PythonTradingExhaustiveCampaignWatchdog"
$Here = $PSScriptRoot
$Root = (Resolve-Path (Join-Path $Here "..\..\..")).Path
if (-not (Test-Path (Join-Path $Root "backtester.py"))) {
    throw "Could not locate stock-bot root from $Here (expected backtester.py)."
}

$PythonCandidates = @(
    (Join-Path (Split-Path -Parent $Root) "venv311\Scripts\pythonw.exe"),
    (Join-Path (Split-Path -Parent $Root) "venv311\Scripts\python.exe"),
    (Join-Path (Split-Path -Parent $Root) ".venv\Scripts\pythonw.exe"),
    (Join-Path (Split-Path -Parent $Root) ".venv\Scripts\python.exe"),
    (Join-Path $Root ".venv\Scripts\pythonw.exe"),
    (Join-Path $Root ".venv\Scripts\python.exe")
) | Where-Object { $_ -and (Test-Path $_) }

if (-not $PythonCandidates) {
    throw "No venv python.exe found."
}
$Python = $PythonCandidates[0]
$Watchdog = Join-Path $Here "campaign_watchdog.py"
if (-not (Test-Path $Watchdog)) {
    throw "Missing $Watchdog"
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed task: $TaskName"
    exit 0
}

# Prefer pythonw so Task Scheduler does not flash a console every 5 minutes.
$Arg = "-u -B `"$Watchdog`""
$Action = New-ScheduledTaskAction -Execute $Python -Argument $Arg -WorkingDirectory $Root
# Every 5 minutes for 10 years (Windows rejects TimeSpan::MaxValue in task XML)
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -Hidden
# Interactive logon (user logged in) + pythonw = no console flash.
# S4U needs elevation on this machine — keep Interactive.
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force | Out-Null

Write-Host "Installed: $TaskName"
Write-Host "  Python:   $Python"
Write-Host "  Watchdog: $Watchdog"
Write-Host "  CWD:      $Root"
Write-Host "  Schedule: every 5 minutes (Hidden + pythonw - no console flash)"
Write-Host "  Status:   $Here\runs\STATUS.md"

if ($RunNow) {
    Write-Host "Running watchdog once (hidden)..."
    $p = Start-Process -FilePath $Python -ArgumentList @("-u","-B",$Watchdog) -WorkingDirectory $Root -WindowStyle Hidden -Wait -PassThru
    Write-Host "Exit: $($p.ExitCode)"
}
