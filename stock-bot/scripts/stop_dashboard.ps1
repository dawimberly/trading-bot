# Stop dashboard monitor processes for this project only.
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    ($_.Name -eq "pythonw.exe" -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root) -and $_.CommandLine -match "dashboard_app\.py") -or
    ($_.Name -eq "PythonTradingMonitor.exe" -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root))
}
if (-not $procs) {
    Write-Host "No dashboard monitor running for $root"
    exit 0
}
foreach ($p in $procs) {
    Write-Host "Stopping $($p.Name) PID $($p.ProcessId)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
