# Read-only Phase 4 Medium paper review, Mondays at 4:15 PM ET.
# Does not restart bots or change PythonTrading_Autonomous_Paper.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ops\install_phase4_monday_review_task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ops\install_phase4_monday_review_task.ps1 -Uninstall

param(
    [string]$AtEt = "16:15",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "PythonTradingPhase4MondayReview"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path (Join-Path $Root "backtester.py"))) {
    $Root = Split-Path -Parent $PSScriptRoot
}
if (-not (Test-Path (Join-Path $Root "backtester.py"))) {
    throw "Could not locate stock-bot root (expected backtester.py)."
}

$PythonCandidates = @(
    (Join-Path (Split-Path -Parent $Root) ".venv\Scripts\python.exe"),
    (Join-Path (Split-Path -Parent $Root) "venv311\Scripts\python.exe"),
    (Join-Path $Root ".venv\Scripts\python.exe")
) | Where-Object { $_ -and (Test-Path $_) }

if (-not $PythonCandidates) {
    throw "No python.exe found."
}
$Python = $PythonCandidates[0]
$Script = Join-Path $Root "scripts\ops\phase4_monday_review.py"
if (-not (Test-Path $Script)) {
    throw "Missing $Script"
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName' (if it existed)."
    exit 0
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
$AtLocal = $localAt.ToString("HH:mm")
$Action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`"" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $AtLocal
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "PythonTrading: read-only Phase 4 Medium paper review, Mondays 4:15 PM ET. Does not restart bots or change the 11 PM paper task." `
    -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Write-Host "  When:    Mondays $AtEt ET ($AtLocal local)"
Write-Host "  Python:  $Python"
Write-Host "  Script:  $Script"
Write-Host "  Report:  $Root\reports\phase4_trial\"
