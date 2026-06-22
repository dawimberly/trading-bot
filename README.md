# PythonTrading

Monorepo for local trading tools. **Only `stock-bot/` is the active trading project.**

| Folder | Purpose |
|--------|---------|
| [`stock-bot/`](stock-bot/) | Alpaca stock/crypto fund bot + portal (active) |
| [`ufc-predictor/`](ufc-predictor/) | UFC fight model + data pipeline (separate project) |

## Quick start

**Daily routine (owner, Live + Paper):** double-click **`Start_Bot_and_Dashboard.bat`** at the repo root. See [stock-bot/README.md — Daily usage](stock-bot/README.md#daily-usage-recommended).

First-time / manual launch:

```bat
cd stock-bot
copy .env.example .env
python scripts\account\preflight.py
```

**Do not use for daily ops:** `start.bat`, `launch.bat`, `launch_both.bat`, or `Weinstein-Trading-Bot.exe` — they start a single bot with the wrong heartbeat paths for the portal dashboard.

Build frozen EXEs (optional): `stock-bot\build_all.bat`

Friend setup: [FRIENDS.md](FRIENDS.md) · Full bot docs: [stock-bot/README.md](stock-bot/README.md)

## Runtime data layout

Legacy state files from repo root were moved into:

- `stock-bot/data/` — runtime state (heartbeats, journals, Kraken/wisdom state, etc.)
- `stock-bot/archive/` — backtest CSVs, jsonl backups, build logs, duplicate scripts

A timestamped backup at `backup_root_purge_2026-06-20/` holds copies of everything moved for easy rollback.
