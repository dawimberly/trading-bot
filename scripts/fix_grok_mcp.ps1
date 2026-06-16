# One-click Grok MCP repair — double-click or: powershell -File scripts\fix_grok_mcp.ps1
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StockBot = Join-Path $Repo "stock-bot"
$VenvPy = Join-Path $Repo ".venv\Scripts\python.exe"
$Cursor = Join-Path $env:LOCALAPPDATA "Programs\cursor\resources\app\bin\cursor.cmd"

Write-Host "=== Grok MCP fix ===" -ForegroundColor Cyan
Write-Host "Repo: $Repo"

if (-not (Test-Path $VenvPy)) {
    Write-Error "Missing venv at $VenvPy"
}

& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $StockBot "scripts\mcp\install_grok_mcp.ps1")

$env:PYTHONUTF8 = "1"
Write-Host ""
Write-Host "Testing Grok API..." -ForegroundColor Yellow
& $VenvPy (Join-Path $StockBot "scripts\mcp\test_grok_mcp.py")
if ($LASTEXITCODE -ne 0) {
    Write-Error "Grok API test failed - check XAI_API_KEY in $Repo\.env"
}

Write-Host ""
Write-Host "Testing MCP server startup..." -ForegroundColor Yellow
$errLog = Join-Path $Repo "logs\grok_mcp_startup.err"
$outLog = Join-Path $Repo "logs\grok_mcp_startup.out"
New-Item -ItemType Directory -Force -Path (Join-Path $Repo "logs") | Out-Null
$env:GROK_CLI_PATH = Join-Path $StockBot "scripts\mcp\grok_cli.cmd"
$p = Start-Process -FilePath $VenvPy -ArgumentList @(
    (Join-Path $StockBot "scripts\mcp\run_grok_mcp.py")
) -WorkingDirectory $StockBot -PassThru -NoNewWindow `
    -RedirectStandardError $errLog -RedirectStandardOutput $outLog
Start-Sleep -Seconds 3
if ($p.HasExited) {
    Write-Host "MCP server failed to start (exit $($p.ExitCode))" -ForegroundColor Red
    if (Test-Path $errLog) { Get-Content $errLog }
    exit 1
}
Stop-Process -Id $p.Id -Force
Write-Host "MCP server starts OK." -ForegroundColor Green

if (Test-Path $Cursor) {
    Write-Host ""
    Write-Host "Registering Grok with Cursor CLI..." -ForegroundColor Yellow
    $pyCode = @"
import json, subprocess
cfg = {
    'name': 'grok',
    'command': r'$VenvPy',
    'args': [r'$(Join-Path $StockBot 'scripts\mcp\run_grok_mcp.py')'],
    'cwd': r'$StockBot',
    'env': {
        'GROK_CLI_PATH': r'$(Join-Path $StockBot 'scripts\mcp\grok_cli.cmd')',
        'PYTHONUTF8': '1',
    },
}
subprocess.run(
    [r'$Cursor', '--add-mcp', json.dumps(cfg), r'$Repo'],
    check=False,
    timeout=15,
)
"@
    & $VenvPy -c $pyCode
    Write-Host "Cursor MCP register step finished (may already exist)." -ForegroundColor Green
}

Write-Host ""
Write-Host "Done. Press Ctrl+Shift+P -> Developer: Reload Window if grok is not green in Settings -> MCP." -ForegroundColor Green
Write-Host "Use model Auto; agent calls grok_query / grok_chat via MCP." -ForegroundColor Green
