# Launch grok-cli-mcp via the Python dotenv wrapper.
$ErrorActionPreference = "Stop"
$launcher = Join-Path $PSScriptRoot "run_grok_mcp.py"
& python $launcher @args
exit $LASTEXITCODE
