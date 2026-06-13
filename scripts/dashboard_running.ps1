# Exit 0 if no dashboard (or stale headless process was cleaned); exit 1 if a live window was focused.
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$startupGraceSec = 45

function Show-DashboardWindow {
    param([System.Diagnostics.Process]$Process)
    if (-not $Process -or $Process.MainWindowHandle -eq [IntPtr]::Zero) {
        return $false
    }
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public class DashboardWin32 {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@ -ErrorAction SilentlyContinue | Out-Null
    [DashboardWin32]::ShowWindow($Process.MainWindowHandle, 9) | Out-Null
    [DashboardWin32]::SetForegroundWindow($Process.MainWindowHandle) | Out-Null
    return $true
}

function Stop-StaleDashboard {
    param(
        [int]$ProcessId,
        [string]$Reason
    )
    Write-Host "[WARN] Stopping stale monitor PID $ProcessId ($Reason)."
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

$procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    ($_.Name -eq "pythonw.exe" -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root) -and $_.CommandLine -match "dashboard_app\.py") -or
    ($_.Name -eq "PythonTradingMonitor.exe" -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root))
})

if (-not $procs) {
    exit 0
}

$focused = $false
$cleaned = 0

foreach ($p in $procs) {
    $dashPid = [int]$p.ProcessId
    $proc = Get-Process -Id $dashPid -ErrorAction SilentlyContinue
    if (-not $proc) {
        continue
    }

    if (Show-DashboardWindow -Process $proc) {
        Write-Host "[INFO] Dashboard already running (PID $dashPid)."
        Write-Host "[INFO] Brought existing window to the front."
        $focused = $true
        break
    }

    $runtimeSec = ((Get-Date) - $proc.StartTime).TotalSeconds
    if ($runtimeSec -lt $startupGraceSec) {
        Write-Host "[INFO] Dashboard starting (PID $dashPid) - wait a few seconds and retry."
        exit 1
    }

    Stop-StaleDashboard -ProcessId $dashPid -Reason "no visible window for $([int]$runtimeSec)s"
    $cleaned++
}

if ($focused) {
    exit 1
}

if ($cleaned -gt 0) {
    Write-Host "[INFO] Cleaned $cleaned stale monitor process(es). Launching fresh instance..."
}

exit 0
