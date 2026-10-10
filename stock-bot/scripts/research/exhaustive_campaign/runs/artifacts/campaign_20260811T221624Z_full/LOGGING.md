# Exhaustive campaign — where everything is logged

Freeze-safe research only. After the power-loss restart (2026-08-11), **phase 4 MC** writes durable per-run files so a crash does not wipe completed runs.

## Live status

| File | Purpose |
|------|---------|
| `STATUS.md` | Watchdog one-pager |
| `campaign_state.json` | Phase, PIDs, cmd, artifact_dir |
| `campaign_heartbeat.json` | Orchestrator heartbeat |
| `campaign_watch_status.json` | Last watchdog check |
| `campaign_master.log` | Orchestrator start/end/retry lines |
| `campaign_detached.log` / `.err` | Detached spawn stdout/stderr |

## Phase logs (verbose console capture)

| Log | Phase |
|-----|--------|
| `phase1_compare_final_365.log` | compare-final |
| `phase2_eval_strict_vs_full_365.log` | STRICT vs FULL |
| `phase3_tod_365.log` | TOD |
| `phase4_mc_200_365.log` | Monte Carlo (full backtest spam) |
| `phase5_walk_forward_4.log` | Walk-forward |
| `phase6_*` / `phase7_*` | Heavy (full profile) |

Prior MC log from power loss: `phase4_mc_200_365.prev_powerloss_*.log` (stopped ~70/200, **no JSONL**).

## Monte Carlo durable export (source of truth for completed runs)

Path (current run):

`artifacts/campaign_20260811T221624Z_full/mc/`

| File | When written |
|------|----------------|
| `meta.json` / `README.md` | At MC start |
| `in_progress.json` / `progress.txt` | At start + each run boundary |
| `runs.jsonl` | **After each completed run** (append + fsync) |
| `summary.json` / `summary.md` | Rolling after each completed run |
| `results.json` | Partial after each run; final at end |

```powershell
Get-Content ...\mc\progress.txt
Get-Content ...\mc\runs.jsonl -Tail 5
Get-Content ...\mc\summary.md
```

**Note:** The MC process started before the latest “log everything” code changes. Markers above fill in as runs complete; **richer start-of-run markers apply on the next MC spawn** (watchdog restart). Completed-run JSONL is already enabled on the live `--export-dir` process.

## Do not lose again

1. Keep AC connected (slow charge OK).
2. Prefer reading `runs.jsonl` / `summary.md` over the huge phase4 console log.
3. If MC dies, watchdog `--resume` should re-enter phase 4 with a **new** export folder under a new `artifact_dir` unless state is fixed — check `campaign_state.json` → `artifact_dir`.
