# PythonTrading

Monorepo for local trading tools. **Only `stock-bot/` is the active trading project.**

| Folder | Purpose |
|--------|---------|
| [`stock-bot/`](stock-bot/) | Alpaca stock/crypto fund bot + portal (active) |
| [`ufc-predictor/`](ufc-predictor/) | UFC fight model + data pipeline (separate project) |

## Quick start

From repo root:

```bat
start.bat
```

`start.bat` prefers `stock-bot\dist\Weinstein-Trading-Bot.exe` when built; otherwise runs `stock-bot\launch.bat` (source Python).

Or directly:

```bat
cd stock-bot
launch.bat
```

**Config:** edit `stock-bot\.env` only — it is authoritative. `dist\.env` is a fallback copy synced on `build_all.bat`.

Build frozen EXEs: `stock-bot\build_all.bat`

Friend setup: [FRIENDS.md](FRIENDS.md) · Full bot docs: [stock-bot/README.md](stock-bot/README.md)

## Runtime data layout

Legacy state files from repo root were moved into:

- `stock-bot/data/` — runtime state (heartbeats, journals, Kraken/wisdom state, etc.)
- `stock-bot/archive/` — backtest CSVs, jsonl backups, build logs, duplicate scripts

A timestamped backup at `backup_root_purge_2026-06-20/` holds copies of everything moved for easy rollback.
