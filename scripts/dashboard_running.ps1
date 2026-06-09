# Exit 0 if no dashboard for this project; exit 1 if already running.
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -eq "pythonw.exe" -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root) -and $_.CommandLine -match "dashboard_app\.py"
}
if ($procs) {
    Write-Host "[INFO] Dashboard already running (PID $($procs[0].ProcessId))."
    exit 1
}
exit 0
