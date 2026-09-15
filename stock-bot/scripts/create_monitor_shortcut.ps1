# Install desktop Paper SoT launcher as a real .exe (custom icon, no shortcut arrow).
param(
    [string]$DesktopDir = ([Environment]::GetFolderPath("Desktop")),
    [switch]$Rebuild
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$icon = Join-Path $root "assets\pythontrading_paper.ico"
$launcherPy = Join-Path $root "scripts\launch_paper_book_desktop.py"
$distExe = Join-Path $root "dist\PythonTradingPaper.exe"
$desktopExe = Join-Path $DesktopDir "PythonTrading Paper.exe"
$py = Join-Path (Split-Path $root -Parent) "venv311\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }

if (-not (Test-Path $icon)) { Write-Error "Missing icon: $icon"; exit 1 }
if (-not (Test-Path $launcherPy)) { Write-Error "Missing launcher: $launcherPy"; exit 1 }
if (-not (Test-Path $py)) { Write-Error "Missing python at $py"; exit 1 }

$needBuild = $Rebuild -or -not (Test-Path $distExe)
if (-not $needBuild) {
    $exeTime = (Get-Item $distExe).LastWriteTimeUtc
    $srcTime = (Get-Item $launcherPy).LastWriteTimeUtc
    $icoTime = (Get-Item $icon).LastWriteTimeUtc
    if ($srcTime -gt $exeTime -or $icoTime -gt $exeTime) { $needBuild = $true }
}

if ($needBuild) {
    Write-Host "Building PythonTradingPaper.exe ..."
    $distDir = Join-Path $root "dist"
    New-Item -ItemType Directory -Force -Path $distDir | Out-Null
    & $py -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name "PythonTradingPaper" `
        --icon $icon `
        --distpath $distDir `
        --workpath (Join-Path $root "build\paper_desktop") `
        --specpath (Join-Path $root "build\paper_desktop") `
        $launcherPy
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if (-not (Test-Path $distExe)) {
    Write-Error "Build finished but missing $distExe"
    exit 1
}

# Remove stale desktop entries (.lnk / .bat / old names).
foreach ($stale in @(
    "PythonTrading Paper.lnk",
    "PythonTrading Paper.bat",
    "PythonTrading Monitor.lnk",
    "Stock-bot.lnk",
    "Paper book.lnk"
)) {
    $stalePath = Join-Path $DesktopDir $stale
    if (Test-Path $stalePath) {
        Remove-Item -LiteralPath $stalePath -Force -ErrorAction SilentlyContinue
        Write-Host "Removed: $stale"
    }
}

Copy-Item -LiteralPath $distExe -Destination $desktopExe -Force
Write-Host "Desktop launcher: $desktopExe"
Write-Host "Opens: dashboard_app.py --book alpaca_paper_v2 (full chrome + cyan tape)"
Write-Host "Icon embedded (no shortcut arrow)."
