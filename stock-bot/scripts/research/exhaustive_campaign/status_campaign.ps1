# Quick status for exhaustive campaign (human-facing).
$ErrorActionPreference = "Continue"
$Here = $PSScriptRoot
$Runs = Join-Path $Here "runs"
Write-Host "=== STATUS.md ==="
if (Test-Path (Join-Path $Runs "STATUS.md")) {
    Get-Content (Join-Path $Runs "STATUS.md")
} else {
    Write-Host "(none yet - run watchdog)"
}
Write-Host ""
Write-Host "=== Live python (campaign-related) ==="
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'run_campaign|compare-final|exhaustive_campaign|eval_strict|monte_carlo|walk_forward|full_strategy|chaotic|backtest_intraday|crypto_vol_v5|run_tod' } |
  ForEach-Object {
    $c = $_.CommandLine
    if ($c.Length -gt 140) { $c = $c.Substring(0,140) + "..." }
    "pid=$($_.ProcessId) $c"
  }
Write-Host ""
Write-Host "=== master log (tail) ==="
if (Test-Path (Join-Path $Runs "campaign_master.log")) {
    Get-Content (Join-Path $Runs "campaign_master.log") -Tail 8
}
