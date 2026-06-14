# Create or update the desktop shortcut for PythonTrading Monitor (.exe launcher).
param(
    [string]$ShortcutPath = (Join-Path ([Environment]::GetFolderPath("Desktop")) "PythonTrading Monitor.lnk")
)

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$launcher = Join-Path $root "launch_monitor.bat"
$icon = Join-Path $root "assets\dashboard.ico"

if (-not (Test-Path $launcher)) {
    Write-Error "Missing launcher: $launcher"
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($ShortcutPath)
$lnk.TargetPath = $launcher
$lnk.WorkingDirectory = $root
$lnk.WindowStyle = 1
$lnk.Description = "PythonTrading Monitor (desktop app + trading bot)"
if (Test-Path $icon) {
    $lnk.IconLocation = "$icon,0"
}
$lnk.Save()

Write-Host "Shortcut saved: $ShortcutPath"
Write-Host "Target: $launcher"
Write-Host "Start in: $root"
