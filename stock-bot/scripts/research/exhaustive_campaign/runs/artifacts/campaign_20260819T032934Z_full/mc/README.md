# Monte Carlo run folder

Durable per-run results so a kill mid-campaign does not lose metrics.

| File | Role |
|------|------|
| `runs.jsonl` | One JSON object per **completed** MC run (append-only, fsync) |
| `summary.json` / `summary.md` | Rolling stats over completed runs |
| `results.json` | Partial after each run; final when complete |
| `progress.txt` / `in_progress.json` | Live progress + current run marker |
| `meta.json` | Run config (seed, window, noise) |

## How to read mid-flight

```text
Get-Content runs.jsonl -Tail 5
Get-Content summary.md
```

Treat results as **partial** until `summary.json` has `"complete": true`.

## This run

- Started (UTC): 2026-08-19T05:42:23.097623+00:00
- Profile: `paper-aggressive, deep-indicators`
- Days: 365 | mc-runs: 200
- Noise: 0.01 | regime_noise: 0.1 | seed: 42
- Window: 2025-11-01 → 2026-08-19
