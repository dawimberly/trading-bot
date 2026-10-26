# Weekday bot health during the NYSE session (America/New_York).
# Restarts dead live, Medium SoT, and Lab. A Lab startup crash is left down.
# Default: every 30 minutes from 9:35 through 16:05 ET.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ops\install_morning_bot_health_task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ops\install_morning_bot_health_task.ps1 -IntervalMinutes 15
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ops\install_morning_bot_health_task.ps1 -Uninstall

param(
    [string]$AtEt = "09:35",
    [string]$UntilEt = "16:05",
    [int]$IntervalMinutes = 30,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "PythonTradingMorningBotHealth"
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
$Script = Join-Path $Root "scripts\ops\morning_bot_health.py"
if (-not (Test-Path $Script)) {
    throw "Missing $Script"
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName' (if it existed)."
    exit 0
}

if ($IntervalMinutes -lt 5) {
    throw "IntervalMinutes must be at least 5."
}

function Convert-EtClockToLocal([string]$Clock) {
    $bits = $Clock.Split(":")
    $h = [int]$bits[0]
    $m = if ($bits.Length -gt 1) { [int]$bits[1] } else { 0 }
    $etNow = [TimeZoneInfo]::ConvertTime([DateTime]::Now, $etZone)
    $etStamp = Get-Date -Year $etNow.Year -Month $etNow.Month -Day $etNow.Day -Hour $h -Minute $m -Second 0
    $etUnspec = [DateTime]::SpecifyKind($etStamp, [DateTimeKind]::Unspecified)
    return [TimeZoneInfo]::ConvertTimeToUtc($etUnspec, $etZone).ToLocalTime()
}

$etZone = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$localAt = Convert-EtClockToLocal $AtEt
$localUntil = Convert-EtClockToLocal $UntilEt
$spanMin = [int]($localUntil - $localAt).TotalMinutes
if ($spanMin -le 0) {
    throw "UntilEt ($UntilEt ET) must be after AtEt ($AtEt ET)."
}
# One extra minute so the UntilEt run is inside the repetition window.
$duration = New-TimeSpan -Minutes ($spanMin + 1)
$AtLocal = $localAt.ToString("HH:mm")

$Arg = "`"$Script`""
$Action = New-ScheduledTaskAction -Execute $Python -Argument $Arg -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $AtLocal
$once = New-ScheduledTaskTrigger -Once -At $localAt -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration $duration
$Trigger.Repetition = $once.Repetition
$Trigger.Repetition.StopAtDurationEnd = $true
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "PythonTrading: weekday bot health every $IntervalMinutes min, $AtEt-$UntilEt ET. Restart dead live, Medium, and Lab. Lab startup crash stays down. No strategy/.env changes." `
    -Force | Out-Null

Write-Host ""
Write-Host "Installed scheduled task: $TaskName"
Write-Host "  When:    Mon-Fri $AtEt-$UntilEt ET every $IntervalMinutes min (first run $AtLocal local)"
Write-Host "  Python:  $Python"
Write-Host "  Script:  $Script"
Write-Host "  Workdir: $Root"
Write-Host "  Log:     $Root\data\morning_bot_health.log"
Write-Host ""
Write-Host "PC must be on (or wake) at that time."
Write-Host "Test now:"
Write-Host "  & `"$Python`" `"$Script`" --dry-run"
Write-Host "  taskschd.msc  ->  $TaskName"
