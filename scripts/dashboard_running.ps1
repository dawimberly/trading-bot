# Exit 0 if no dashboard for this project; exit 1 if already running (focus existing window).
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

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

$procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    ($_.Name -eq "pythonw.exe" -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root) -and $_.CommandLine -match "dashboard_app\.py") -or
    ($_.Name -eq "PythonTradingMonitor.exe" -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root))
})

if (-not $procs) {
    exit 0
}

$pid = $procs[0].ProcessId
Write-Host "[INFO] Dashboard already running (PID $pid)."

$proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
if ($proc -and (Show-DashboardWindow -Process $proc)) {
    Write-Host "[INFO] Brought existing window to the front."
} else {
    Write-Host "[INFO] Window may be minimized to the system tray - click the PythonTrading icon near the clock."
}

exit 1
