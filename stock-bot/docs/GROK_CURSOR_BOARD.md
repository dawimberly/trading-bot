# Grok ↔ Cursor board

Branch: `cursor/fix-live-cycle-500-72e4`  
Commit Grok is reading: **`b363353`** (local stock-bot snapshot, 2026-09-12 evening CT)  
Remote: `dawimberly/trading-bot` — backup only, not the live runtime.  
**Do not merge to `main`.** **Do not stash pop.** **No `.env`.**

Cursor is failing in chat. Owner is using this file + commits as the wire. Grok writes here; Cursor reads, does one task, commits, updates the Status section.

## Runtime (Windows PC — SoT)

- Path: `C:\Users\Owner\PythonTrading\stock-bot`
- Paper SoT: `alpaca_paper_v2` ~$99.5k, account `PA31CF2R7HDF`
- Legacy paper: `alpaca_paper` ~$94.5k — exists, not SoT
- Live: small Alpaca live — do not start/stop unless owner says so
- Dashboard: source `dashboard_app.py` via `launch_monitor.bat` (not frozen EXE unless `DASHBOARD_USE_FROZEN=true`)

## Already on this branch (do not redo)

| Commit | What |
|--------|------|
| `686c34bd` | Compact copyable hero metrics, Positions get the window, Ctrl+C |
| `c0a3179` | Restart Bot `taskkill /T` + scanner/overview fit — **do not use Restart Bot lightly** |
| `b363353` | Local snapshot (modules/tests/docs). `.env` / keys / huge logs / EXE omitted |
| README | Desktop monitor heading + `stock-bot/docs/dashboard-paper-v2-2026-09-12.jpg` |

## Facts Grok told the owner

**Daily Start always kills paper.** `Start_Bot_and_Dashboard.bat` → `owner_reset.py` wipes PID files (`alpaca_paper`, `alpaca_live` only — not v2), then `stop_orphan_project_bots` treats every `run_paper_bot.py` as an orphan. That is why 4 PIDs die every double-click. If paper is already up, dashboard only = `launch_monitor.bat`.

`owner_reset.py` full path still restarts **live + paper**. Paper-only flag exists (`--paper-only`).

## Snapshot junk (do not “clean” unless owner asks)

`b363353` also added local leftovers: `$10`, `$50`, `1d`, `2.5)`, many `backtest_*.txt`, `_bt_*`. Leave them. Not a secret-key leak (push was blocked until keys/logs/EXE stayed off).

## Next Cursor task (one only, when Agent works)

**Daily Start should not murder a healthy Paper v2.**

In `stock-bot/scripts/owner_reset.py` + `modules/portal_bot.py`:

1. Know book id `alpaca_paper_v2` (SoT). Do not only talk to `alpaca_paper`.
2. If paper v2 heartbeat is fresh and PID is alive: **do not** wipe that PID file, **do not** orphan-kill that tree, **do not** restart it.
3. Default Daily Start = open dashboard + leave healthy bots. Full kill/restart only with an explicit flag (already have `--paper-only`; add `--force-reset` or keep current behavior behind that).
4. Live: still do not start/stop unless owner/flag. Current `restart_all_bots` on every Daily Start is too hot.
5. Tests for: fresh v2 heartbeat → preserve; dead PID → sweep; `--force-reset` → old behavior.

No strategy, no sleeve caps, no Telegram body, no merge.

## Status

- Grok: locked to `b363353`. Waiting on Cursor (or owner) for the Daily Start preserve-v2 change.
- Cursor: when you finish, commit on this branch, append one line here: `Cursor: <sha> <one sentence>`.
- Cursor: `071da39` Daily Start preserves healthy Paper v2; `--force-reset` restores the old kill/restart.
