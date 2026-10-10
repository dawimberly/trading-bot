# Campaign artifacts (`full`)

Created: 2026-08-06 (seeded while MC 200 already in flight)

Research-only. Freeze / paper / live are not wired from this folder.

## Layout

| Path | Role |
|------|------|
| `mc/` | Monte Carlo durable exports (`runs.jsonl`, `summary.md`, …) |
| `../phaseN_*.log` | Phase stdout (sibling under `runs/`) |
| `MANIFEST.json` | Profile + paths snapshot |

## Important — current MC 200

The **in-flight** Monte Carlo process was started **before** `--export-dir` existed.
It will **not** write `mc/runs.jsonl` (old code already in memory).

- Do **not** kill/restart just for logging — that would reset ~hours of progress.
- Per-run JSONL starts on the **next** MC spawn (watchdog `--resume` or a new campaign).
- Phases 1–3 results already live under `scripts/analysis/` (see MANIFEST).

## Reading MC mid-flight (once JSONL exists)

```powershell
Get-Content 'mc\summary.md'
Get-Content 'mc\runs.jsonl' -Tail 5
```

Partial until `mc/summary.json` has `"complete": true`.
