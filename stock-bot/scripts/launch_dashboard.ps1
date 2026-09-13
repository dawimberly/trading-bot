# Launch one dashboard_app.py via base pythonw + venv site-packages (no venv stub ghost window).
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$runningPs1 = Join-Path $PSScriptRoot "dashboard_running.ps1"
& $runningPs1
if ($LASTEXITCODE -eq 1) {
    exit 0
}

$venvPyCandidates = @(
    (Join-Path (Split-Path $Root -Parent) "venv311\Scripts\python.exe"),
    (Join-Path $Root ".venv\Scripts\python.exe"),
    (Join-Path (Split-Path $Root -Parent) ".venv\Scripts\python.exe")
)
$venvPy = $venvPyCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $venvPy) {
    Write-Host "[ERROR] No venv python.exe found."
    exit 1
}

$pyw = & $venvPy -c "import sys; from pathlib import Path; print(Path(sys.base_prefix) / 'pythonw.exe')"
$pyw = ($pyw | Select-Object -Last 1).ToString().Trim()
if (-not (Test-Path $pyw)) {
    Write-Host "[ERROR] Base pythonw not found: $pyw"
    exit 1
}

$venvRoot = & $venvPy -c "import sys; print(sys.prefix)"
$venvRoot = ($venvRoot | Select-Object -Last 1).ToString().Trim()
$site = Join-Path $venvRoot "Lib\site-packages"

$env:PYTHONTRADING_ROOT = $Root
$env:VIRTUAL_ENV = $venvRoot
$env:PYTHONPATH = $site
$script = Join-Path $Root "dashboard_app.py"

Write-Host "[INFO] Using source monitor: $pyw $script"
Start-Process -FilePath $pyw -ArgumentList $script -WorkingDirectory $Root
exit 0
