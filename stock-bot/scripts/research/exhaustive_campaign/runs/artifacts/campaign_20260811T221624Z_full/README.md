# Campaign artifacts (`full`)

Created: 2026-08-11 22:16:24 UTC

Research-only. Freeze / paper / live are not wired from this folder.

## Layout

| Path | Role |
|------|------|
| `mc/` | Monte Carlo durable exports (`runs.jsonl`, `summary.md`, …) |
| `../phaseN_*.log` | Phase stdout (sibling under `runs/`) |
| `MANIFEST.json` | Profile + paths snapshot |

## Reading MC mid-flight

```powershell
Get-Content 'C:\Users\Owner\PythonTrading\stock-bot\scripts\research\exhaustive_campaign\runs\artifacts\campaign_20260811T221624Z_full\mc\summary.md'
Get-Content 'C:\Users\Owner\PythonTrading\stock-bot\scripts\research\exhaustive_campaign\runs\artifacts\campaign_20260811T221624Z_full\mc\runs.jsonl' -Tail 5
```

Partial until `mc/summary.json` has `"complete": true`.
