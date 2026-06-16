# Sync Grok MCP config into Cursor (user + project). Run from repo root.
$ErrorActionPreference = "Stop"
$StockBot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
# Monorepo: venv + .env often live one level above stock-bot/
$Root = $StockBot
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    $Root = (Resolve-Path (Join-Path $StockBot "..")).Path
    $VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
}
$Launcher = Join-Path $StockBot "scripts\mcp\run_grok_mcp.py"
$GrokCmd = Join-Path $StockBot "scripts\mcp\grok_cli.cmd"
$UserMcp = Join-Path $env:USERPROFILE ".cursor\mcp.json"
$ProjectMcp = Join-Path $Root ".cursor\mcp.json"

if (-not (Test-Path $VenvPy)) {
    Write-Error "Missing venv python at $VenvPy — run: python -m venv .venv; pip install -r requirements.txt"
}
if (-not (Test-Path (Join-Path $env:USERPROFILE ".grok\bin\grok.exe"))) {
    Write-Warning "grok.exe not found at %USERPROFILE%\.grok\bin\grok.exe — run: irm https://x.ai/cli/install.ps1 | iex"
}

$mcp = @{
    mcpServers = @{
        grok = @{
            command = $VenvPy
            args = @($Launcher)
            cwd = $StockBot
            env = @{
                GROK_CLI_PATH = $GrokCmd
                PYTHONUTF8 = "1"
            }
        }
    }
} | ConvertTo-Json -Depth 6

New-Item -ItemType Directory -Force -Path (Split-Path $UserMcp) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path $ProjectMcp) | Out-Null
$mcp | Set-Content -Path $UserMcp -Encoding utf8
$mcp | Set-Content -Path $ProjectMcp -Encoding utf8

Write-Host "Wrote Grok MCP config to:"
Write-Host "  $UserMcp"
Write-Host "  $ProjectMcp"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Cursor -> Settings -> MCP -> Restart grok server (or Reload Window)"
Write-Host "  2. Use model: Auto (NOT Grok in the model dropdown)"
Write-Host "  3. Disable heavy MCP servers (GitKraken/GitLens) if you still see tool errors"
Write-Host "  4. Super Grok (grok.com trial): scripts/mcp/copy_manifest_for_grok.ps1 then paste at grok.com"
Write-Host "  5. Cursor MCP API: grok login OR API credits at https://console.x.ai"
Write-Host "  6. Test: python scripts/mcp/test_grok_mcp.py"
Write-Host "  7. Ask: @Grok ... (uses patched scripts/mcp/grok_mcp; restart MCP after updates)"
