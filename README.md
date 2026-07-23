# PythonTrading

Monorepo for local trading tools. **Only `stock-bot/` is the active trading project.**

| Folder | Purpose |
|--------|---------|
| [`stock-bot/`](stock-bot/) | Alpaca stock/crypto fund bot + portal (active) |
| [`ufc-predictor/`](ufc-predictor/) | UFC fight model + data pipeline (separate project) |

**Repo (personal):** [github.com/dawimberly/trading-bot](https://github.com/dawimberly/trading-bot)  
**Repo (group / infinite-robots):** [github.com/infinite-robots/python-trading](https://github.com/infinite-robots/python-trading)

Both remotes were synced to the same commits on `main` and all cursor branches (starting point: `03abb6c`). They are **independent from here on** — updates to one are not automatic on the other.

| Remote | GitHub | Who |
|--------|--------|-----|
| `origin` | `dawimberly/trading-bot` | Your sandbox — default `git push` / `git pull` |
| `infinite-robots` | `infinite-robots/python-trading` | Group clone — push when you want to share |

**You (after clone with both remotes):** `git pull origin main` · `git push origin main` · share to group: `git push infinite-robots main`  
**Group:** `git clone https://github.com/infinite-robots/python-trading.git` only — do not use the personal URL.

Add the org remote once: `git remote add infinite-robots https://github.com/infinite-robots/python-trading.git`

## Quick start

**Daily routine (owner, Live + Paper):** double-click **`Start_Bot_and_Dashboard.bat`** at the repo root. See [stock-bot/README.md — Daily usage](stock-bot/README.md#daily-usage-recommended).

**Environment:** copy [`stock-bot/.env.example`](stock-bot/.env.example) → `stock-bot/.env` (placeholders only — never commit real keys).

First-time / manual launch:

```bat
cd stock-bot
copy .env.example .env
python scripts\account\preflight.py
```

**Python 3.11 venv (owner):** from `stock-bot/`, run `scripts\setup_venv.bat` once → `C:\Users\Owner\PythonTrading\venv311`; daily `scripts\activate_venv.bat`. Details: [stock-bot/README — First-time setup](stock-bot/README.md#first-time-setup).

**Do not use for daily ops:** `start.bat`, `launch.bat`, `launch_both.bat`, or `Weinstein-Trading-Bot.exe` — they start a single bot with the wrong heartbeat paths for the portal dashboard.

Build frozen EXEs (optional): `stock-bot\build_all.bat`

Friend setup: [FRIENDS.md](FRIENDS.md) · Full bot docs: [stock-bot/README.md](stock-bot/README.md) — see **“What the bot is set to do (runtime defaults)”** for live vs paper vs research-only features (good to paste into Grok with `python status.py` output).

## Runtime data layout

Legacy state files from repo root were moved into:

- `stock-bot/data/` — runtime state (heartbeats, journals, Kraken/wisdom state, etc.)
- `stock-bot/archive/` — backtest CSVs, jsonl backups, build logs, duplicate scripts

A timestamped backup at `backup_root_purge_2026-06-20/` holds copies of everything moved for easy rollback.
