# Copy ultra bot manifest + review prompt to clipboard for grok.com Super Grok.
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
& "$Root\.venv\Scripts\python.exe" "$Root\scripts\mcp\export_bot_manifest.py" --ultra | Out-Null
$manifest = Get-Content "$Root\data\bot_manifest.txt" -Raw
$prompt = @"
Review this systematic trading bot from BOT_MANIFEST only (~2k tokens, not full source).
Return: (1) one-paragraph summary (2) live vs paper (3) control flow (4) top 5 risks (5) 3 improvements.
Cite manifest paths.

--- BOT_MANIFEST ---
$manifest
--- END ---
"@
Set-Clipboard -Value $prompt
Write-Host "Copied manifest + prompt to clipboard. Paste into https://grok.com (Super Grok)."
