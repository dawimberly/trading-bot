# Create or repair desktop shortcuts for UFC Predictor.
# GUI shortcuts target dist\ufc-dashboard.exe directly (no terminal flash).
param(
    [string]$Desktop = [Environment]::GetFolderPath("Desktop"),
    [string]$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$shell = New-Object -ComObject WScript.Shell

function Save-Shortcut {
    param(
        [string]$Path,
        [string]$Target,
        [string]$WorkingDir,
        [string]$Description,
        [string]$Arguments = ""
    )
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath = $Target
    $lnk.WorkingDirectory = $WorkingDir
    $lnk.Arguments = $Arguments
    $lnk.WindowStyle = 1
    $lnk.Description = $Description
    $lnk.Save()
    Write-Host "Shortcut: $Path"
    Write-Host "  Target: $Target $Arguments"
    Write-Host "  Start in: $WorkingDir"
}

$distDir = Join-Path $Root "dist"
$dashboardExe = Join-Path $distDir "ufc-dashboard.exe"
$predictBat = Join-Path $Root "launch_predict.bat"

if (-not (Test-Path $dashboardExe)) {
    Write-Error "Missing $dashboardExe - run build_dashboard.bat first."
    exit 1
}

# GUI - launch windowed EXE directly (no cmd window)
$guiShortcuts = @(
    (Join-Path $Desktop "UFC Dashboard.lnk"),
    (Join-Path $Desktop "ufc-predict - Shortcut.lnk")
)
foreach ($path in $guiShortcuts) {
    Save-Shortcut `
        -Path $path `
        -Target $dashboardExe `
        -WorkingDir $distDir `
        -Description "UFC Predictor GUI dashboard"
}

# CLI - optional terminal analyzer
if (Test-Path $predictBat) {
    Save-Shortcut `
        -Path (Join-Path $Desktop "UFC Predict CLI.lnk") `
        -Target $predictBat `
        -WorkingDir $Root `
        -Description "UFC Predict terminal analyzer (--next-two --odds)"
}

# Remove misleading old CLI shortcut name if present
$oldCli = Join-Path $Desktop "UFC Predict.lnk"
if (Test-Path $oldCli) {
    $s = $shell.CreateShortcut($oldCli)
    if ($s.TargetPath -match 'launch_predict') {
        Remove-Item $oldCli -Force
        Write-Host "Removed old UFC Predict.lnk (was CLI-only; use UFC Dashboard for GUI)"
    }
}

Write-Host ""
Write-Host "Done. Use 'UFC Dashboard' or 'ufc-predict - Shortcut' for the GUI."
Write-Host "Desktop: $Desktop"
