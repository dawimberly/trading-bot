# Create or update the desktop shortcut for Stock-bot (Paqinhaus monitor).
param(
    [string]$ShortcutPath = (Join-Path ([Environment]::GetFolderPath("Desktop")) "Stock-bot.lnk")
)

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$launcher = Join-Path $root "launch_monitor.bat"
$icon = Join-Path $root "assets\dashboard.ico"

if (-not (Test-Path $launcher)) {
    Write-Error "Missing launcher: $launcher"
    exit 1
}

if (-not (Test-Path $icon)) {
    Write-Host "Building icon..."
    & python (Join-Path $PSScriptRoot "make_paqinhaus_icon.py")
}

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($ShortcutPath)
$lnk.TargetPath = $launcher
$lnk.WorkingDirectory = $root
$lnk.WindowStyle = 7
$lnk.Description = "Stock-bot - PythonTrading Monitor (Paqinhaus)"
if (Test-Path $icon) {
    $lnk.IconLocation = "$icon,0"
}
$lnk.Save()

$legacy = Join-Path ([Environment]::GetFolderPath("Desktop")) "PythonTrading Monitor.lnk"
if ((Test-Path $legacy) -and ($legacy -ne $ShortcutPath)) {
    Remove-Item $legacy -Force -ErrorAction SilentlyContinue
    Write-Host "Removed legacy: $legacy"
}

Write-Host "Shortcut saved: $ShortcutPath"
Write-Host "Target: $launcher"
Write-Host "Icon: $icon"
