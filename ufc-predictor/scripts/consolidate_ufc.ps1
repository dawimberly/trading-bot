# Keep one canonical UFC project (C:\UFC-Predictor) and sync the monorepo copy.
# Usage:
#   powershell -File scripts\consolidate_ufc.ps1
#   powershell -File scripts\consolidate_ufc.ps1 -MonorepoRoot C:\Users\Owner\PythonTrading

param(
    [string]$CanonicalRoot = "C:\UFC-Predictor",
    [string]$MonorepoRoot = "C:\Users\Owner\PythonTrading"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $CanonicalRoot)) {
    throw "Canonical UFC root not found: $CanonicalRoot"
}

$MonoPredictor = Join-Path $MonorepoRoot "ufc-predictor"
$MonoBot = Join-Path $MonorepoRoot "ufc_betting_bot"

Write-Host "=== UFC consolidation ===" -ForegroundColor Cyan
Write-Host "Canonical: $CanonicalRoot"
Write-Host "Monorepo:  $MonorepoRoot"

# Sync code + config into monorepo ufc-predictor (excludes heavy build artifacts)
if (Test-Path $MonoPredictor) {
    & robocopy $CanonicalRoot $MonoPredictor /MIR /NFL /NDL /NJH /NJS /nc /ns /np `
        /XD build dist .pytest_cache __pycache__ cachedir .git | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy to ufc-predictor failed: $LASTEXITCODE" }
    Write-Host "Synced -> $MonoPredictor" -ForegroundColor Green
} else {
    Write-Warning "Monorepo ufc-predictor missing; skipping sync."
}

# Sync vendored betting bot package both ways (canonical wins)
if (Test-Path $MonoBot) {
    & robocopy (Join-Path $CanonicalRoot "ufc_betting_bot") $MonoBot /E /NFL /NDL /NJH /NJS /nc /ns /np `
        /XD tests __pycache__ Images .pytest_cache | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy ufc_betting_bot failed: $LASTEXITCODE" }
    Write-Host "Synced ufc_betting_bot -> monorepo" -ForegroundColor Green
}

# Ensure canonical root .env exists (copy from dist if user only edited dist/.env)
$rootEnv = Join-Path $CanonicalRoot ".env"
$distEnv = Join-Path $CanonicalRoot "dist\.env"
if (-not (Test-Path $rootEnv) -and (Test-Path $distEnv)) {
    Copy-Item $distEnv $rootEnv
    Write-Host "Created $rootEnv from dist\.env" -ForegroundColor Yellow
}

# Point monorepo betting bot at canonical predictor
$botEnv = Join-Path $MonoBot ".env"
$canonicalLine = "UFC_CANONICAL_ROOT=$CanonicalRoot"
if (Test-Path $botEnv) {
    $content = Get-Content $botEnv -Raw
    if ($content -notmatch "UFC_CANONICAL_ROOT=") {
        Add-Content $botEnv "`n$canonicalLine"
        Write-Host "Added UFC_CANONICAL_ROOT to $botEnv" -ForegroundColor Yellow
    }
} elseif (Test-Path (Join-Path $MonoBot ".env.example")) {
    Copy-Item (Join-Path $MonoBot ".env.example") $botEnv
    Add-Content $botEnv "`n$canonicalLine"
    Write-Host "Created $botEnv from example" -ForegroundColor Yellow
}

# Enable props on canonical root .env if missing
if (Test-Path $rootEnv) {
    $lines = Get-Content $rootEnv
    if ($lines -notmatch "^ENABLE_PROPS=") {
        Add-Content $rootEnv "ENABLE_PROPS=true"
        Write-Host "Added ENABLE_PROPS=true to $rootEnv" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Done. Use only:" -ForegroundColor Green
Write-Host "  $CanonicalRoot"
Write-Host "  .env location: $rootEnv"
Write-Host "  Dashboard EXE: $(Join-Path $CanonicalRoot 'dist\ufc-dashboard.exe')"
