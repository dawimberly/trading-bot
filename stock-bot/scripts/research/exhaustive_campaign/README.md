# Exhaustive research campaign (freeze-safe)

**Purpose:** Use every serious tool in this bot to hunt for *robust* edges under
OOS / walk-forward / MC / chaos. Research and measure only.

**Retirement-fund bar:** Overnight jobs must be durable. Do **not** run the
campaign as a Cursor agent background shell. Use the detached runner + Task
Scheduler watchdog below.

## Hard rules

- No `.env` / paper / live wiring from this campaign.
- Prefer OOS, walk-forward, MC, chaos — reject one-window Sharpe heroes.
- Logs + state under `runs/`.
- Sneaky Pivot / RHYME vol fixes stay sidecars until explicit promote review.

## Durable ops (required)

| Piece | Path | Role |
|-------|------|------|
| Orchestrator | `run_campaign.py` | Phases 1–9 with retries, heartbeat, state, single-instance lock |
| Watchdog | `campaign_watchdog.py` | Every 5 min: if dead mid-run → `--resume` detached restart |
| Task install | `install_campaign_watchdog_task.ps1` | Registers `PythonTradingExhaustiveCampaignWatchdog` |
| Human status | `status_campaign.ps1` / `runs/STATUS.md` | What is alive right now |

### Install / verify

```powershell
cd C:\Users\Owner\PythonTrading\stock-bot
powershell -ExecutionPolicy Bypass -File scripts\research\exhaustive_campaign\install_campaign_watchdog_task.ps1 -RunNow
powershell -ExecutionPolicy Bypass -File scripts\research\exhaustive_campaign\status_campaign.ps1
```

### State files in `runs/`

- `campaign_state.json` — phase, status (`running` / `halted` / `completed`)
- `campaign_heartbeat.json` — refreshed ~20s while orchestrator lives
- `campaign.pid` / `campaign.lock` — single-instance
- `campaign_master.log` — append-only event log
- `watchdog.log` / `STATUS.md` — watchdog decisions
- `phaseN_*.log` — per-phase stdout
- `artifacts/campaign_<utc>_<profile>/` — durable results (see folder README)
  - `mc/runs.jsonl` — one JSON object per completed Monte Carlo run
  - `mc/summary.md` — rolling mean/p5/p95 (partial until complete)

### Failure policy

- **Critical phases (1,2,4,5):** retry 3× total, then **HALT** (no silent cascade).
- **Non-critical (3,6–9):** retry, then continue; failures recorded for synthesis.
- Phase 1 also requires **log success markers**, not just exit code 0.
- `status=halted` is **not** auto-restarted — needs human inspection.

## Phases

| Phase | Command | Notes |
|------:|---------|-------|
| 1 | `backtester.py --compare-final --days 365 --regime-breakdown` | Critical |
| 2 | STRICT vs FULL 365d | Critical |
| 3 | TOD 365d | |
| 4 | Monte Carlo 200 | Critical / long |
| 5 | Walk-forward 4 | Critical |
| 6 | `full_strategy_experiment --phase all` | Long |
| 7 | Chaos scenarios | |
| 8 | Intraday 5m 90d | |
| 9 | Crypto vol v5 90d | |
| 10 | Synthesis stub | Written at end |

## Honesty check

Renaissance-style edge needs industrial process + unique data + years.
This campaign maximizes *what this codebase can prove*. It will not invent alpha from noise.
