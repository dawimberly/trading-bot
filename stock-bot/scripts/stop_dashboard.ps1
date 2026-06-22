# Stop dashboard monitor processes for stock-bot only.
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    ($_.Name -match '^(pythonw|python)\.exe$' -and $_.CommandLine -match 'dashboard_app\.py' -and (
            ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($root)) -or
            ($_.CommandLine -like "*$root*")
        )) -or
    ($_.Name -eq 'PythonTradingMonitor.exe' -and (
            ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($root)) -or
            ($_.CommandLine -like "*$root*")
        ))
})

if (-not $procs) {
    Write-Host "No dashboard monitor running (OK if this is a fresh start)."
    exit 0
}

foreach ($p in $procs) {
    Write-Host "Stopping $($p.Name) PID $($p.ProcessId)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

Write-Host "Dashboard stopped."
