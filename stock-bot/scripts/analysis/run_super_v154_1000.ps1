# Super backtest: Realistic Research v1.5.4 paper-aggressive
# Expanded universe (200+) + walk-forward 5 + Monte Carlo 100
# Sequential phases to avoid CPU pile-ups. Do not commit.

$ErrorActionPreference = "Continue"
$Root = "c:\Users\Owner\PythonTrading"
if (-not (Test-Path (Join-Path $Root "stock-bot\backtester.py"))) {
    $Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
}
$Bot = Join-Path $Root "stock-bot"
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }

Set-Location $Bot

# Expanded trading pool (paper locks honor explicit env)
$env:BASE_UNIVERSE_SIZE = "160"
$env:SECTOR_EXPANSION_SIZE = "70"
$env:SECTOR_MAX_TOTAL_TICKERS = "220"
$env:SECTOR_FALLBACK_MOMENTUM_COUNT = "55"
$env:MAX_ACTIVE_SECTORS_STRONG = "4"
$env:DYNAMIC_SECTOR_SCREENER_ENABLED = "true"
# Isolate speed: HMM soft off (note in summary)
$env:MARKOV_HMM_ENABLED = "false"
$env:MARKOV_HMM_PRIMARY_REGIME = "false"
$env:PYTHONUNBUFFERED = "1"

$Stamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ss"
$Status = Join-Path $Bot "backtest_v154_super_1000.status.txt"
$PidFile = Join-Path $Bot "backtest_v154_super_1000.pid.txt"
$Summary = Join-Path $Bot "scripts\analysis\backtest_v154_super_1000_summary.txt"

function Write-Status([string]$phase, [string]$detail) {
    @"
phase=$phase
detail=$detail
updated=$(Get-Date -Format o)
pid=$PID
"@ | Set-Content -Path $Status -Encoding utf8
}

"$PID | started=$Stamp | py=$Py" | Set-Content -Path $PidFile -Encoding utf8
Write-Status "init" "Super suite starting"

$t0 = Get-Date
@"
=== SUPER BACKTEST v1.5.4 paper-aggressive ===
Started: $Stamp
Python: $Py
Env: BASE=$($env:BASE_UNIVERSE_SIZE) EXP=$($env:SECTOR_EXPANSION_SIZE) MAX=$($env:SECTOR_MAX_TOTAL_TICKERS) FALLBACK=$($env:SECTOR_FALLBACK_MOMENTUM_COUNT)
HMM: OFF (isolation/speed) | thinking: OFF
Baseline (current thorough 1000d): +53.36% / Sharpe 1.02 / MaxDD -16.23% / vs VTI +7.83 pp
"@ | Set-Content -Path $Summary -Encoding utf8

# --- Phase 1: Expanded 1000d ---
Write-Status "phase1_expanded_1000d" "running"
$out1 = Join-Path $Bot "backtest_v154_super_1000_expanded.txt"
$err1 = Join-Path $Bot "backtest_v154_super_1000_expanded.err.txt"
Write-Host "=== PHASE 1: Expanded 1000d ==="
& $Py -u backtester.py --days 1000 --paper-aggressive --no-thinking --export-json "scripts/analysis/backtest_v154_super_1000_expanded.json" 1> $out1 2> $err1
$rc1 = $LASTEXITCODE
$elapsed1 = [math]::Round(((Get-Date) - $t0).TotalMinutes, 1)
Add-Content $Summary "`nPHASE1 exit=$rc1 elapsed_min=$elapsed1 out=$out1"
if ($rc1 -ne 0) {
    Write-Status "phase1_FAILED" "exit=$rc1"
    exit $rc1
}
Write-Status "phase1_done" "elapsed_min=$elapsed1"

# --- Phase 2: Walk-forward 5 folds ---
$t2 = Get-Date
Write-Status "phase2_walk_forward_5" "running"
$out2 = Join-Path $Bot "backtest_v154_super_1000_wf5.txt"
$err2 = Join-Path $Bot "backtest_v154_super_1000_wf5.err.txt"
Write-Host "=== PHASE 2: Walk-forward 5 folds ==="
& $Py -u scripts/analysis/walk_forward.py --days 1000 --paper-aggressive --no-thinking --walk-steps 5 --export-json "scripts/analysis/backtest_v154_super_1000_wf5.json" 1> $out2 2> $err2
$rc2 = $LASTEXITCODE
$elapsed2 = [math]::Round(((Get-Date) - $t2).TotalMinutes, 1)
Add-Content $Summary "`nPHASE2 exit=$rc2 elapsed_min=$elapsed2 out=$out2"
if ($rc2 -ne 0) {
    Write-Status "phase2_FAILED" "exit=$rc2"
    exit $rc2
}
Write-Status "phase2_done" "elapsed_min=$elapsed2"

# --- Phase 3: Monte Carlo 100 ---
$t3 = Get-Date
Write-Status "phase3_monte_carlo_100" "running"
$out3 = Join-Path $Bot "backtest_v154_super_1000_mc100.txt"
$err3 = Join-Path $Bot "backtest_v154_super_1000_mc100.err.txt"
Write-Host "=== PHASE 3: Monte Carlo 100 ==="
& $Py -u scripts/analysis/monte_carlo_backtest.py --days 1000 --paper-aggressive --no-thinking --mc-runs 100 --seed 42 --export-json "scripts/analysis/backtest_v154_super_1000_mc100.json" 1> $out3 2> $err3
$rc3 = $LASTEXITCODE
$elapsed3 = [math]::Round(((Get-Date) - $t3).TotalMinutes, 1)
$totalMin = [math]::Round(((Get-Date) - $t0).TotalMinutes, 1)
Add-Content $Summary "`nPHASE3 exit=$rc3 elapsed_min=$elapsed3 out=$out3"
Add-Content $Summary "`nTOTAL_elapsed_min=$totalMin finished=$(Get-Date -Format o)"
if ($rc3 -ne 0) {
    Write-Status "phase3_FAILED" "exit=$rc3"
    exit $rc3
}
Write-Status "DONE" "total_min=$totalMin"
Write-Host "=== SUPER SUITE COMPLETE ($totalMin min) ==="
exit 0
