# Copy UFC Predictor to a standalone folder outside PythonTrading.
# Usage:  powershell -File scripts\migrate_standalone.ps1
#         powershell -File scripts\migrate_standalone.ps1 -Destination C:\UFC-Bot

param(
    [string]$Destination = "C:\UFC-Predictor"
)

$ErrorActionPreference = "Stop"
$Source = Split-Path $PSScriptRoot -Parent
$Monorepo = Split-Path $Source -Parent
$BettingBotSrc = Join-Path $Monorepo "ufc_betting_bot"

Write-Host "=== UFC Predictor standalone migration ===" -ForegroundColor Cyan
Write-Host "Source:      $Source"
Write-Host "Destination: $Destination"

if (-not (Test-Path $Source)) {
    throw "Source not found: $Source"
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null

& robocopy $Source $Destination /E /NFL /NDL /NJH /NJS /nc /ns /np /XD build .pytest_cache __pycache__ | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit $LASTEXITCODE" }

$bbDest = Join-Path $Destination "ufc_betting_bot"
New-Item -ItemType Directory -Force -Path $bbDest | Out-Null
if (Test-Path $BettingBotSrc) {
    & robocopy $BettingBotSrc $bbDest /E /NFL /NDL /NJH /NJS /nc /ns /np /XD tests __pycache__ Images .pytest_cache | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "ufc_betting_bot copy failed" }
    Write-Host "Copied ufc_betting_bot package" -ForegroundColor Green
} else {
    Write-Warning "ufc_betting_bot not found at $BettingBotSrc - copy manually."
}

New-Item -ItemType Directory -Force -Path (Join-Path $Destination "dist") | Out-Null

Write-Host ""
Write-Host "Done. Standalone project:" -ForegroundColor Green
Write-Host "  $Destination"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  cd $Destination"
Write-Host "  build_exe.bat"
Write-Host "  build_dashboard.bat"
