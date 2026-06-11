# Cloud Bot

**First working release** — runs the same **Best Paper Bot** stack as the laptop paper bot, isolated under `cloud_bot/` for 24/7 VPS deployment.

**Deployment:** [`README_CLOUD.md`](README_CLOUD.md) | Overview: [`../README_CLOUD.md`](../README_CLOUD.md)

## Stack (matches final paper backtest)

- Dynamic VTI (40–75%)
- Dynamic risk (1–3%)
- Statistical arbitrage (cointegration, both legs)
- Volatility overlay
- Options income (covered calls)
- Macro regime adaptor
- Advanced flags: overlap, adaptive chunk, co-fire

Implementation lives in the **parent repo** (`run_all.py`, `modules/`). Cloud bot applies profile overrides and cloud paths — no duplicate strategy code.

## Layout

```
cloud_bot/
├── config/
│   ├── profile.py       # Best paper env + config overrides
│   └── settings.py      # Paths, logging, heartbeat
├── runtime/
│   ├── main.py          # Entry: trade loop or backtest
│   ├── loop.py          # 24/7 run_all.py supervisor
│   ├── backtest.py      # Backtest wrapper
│   └── logging_setup.py
├── modules/stack.py     # Stack documentation
├── deploy/systemd/cloud-bot.service
├── data/                # Heartbeat, journal, logs (gitignored)
└── .env.example
```

## Quick start (local)

From repo root (recommended):

```bash
# Dry-run config check
python -m cloud_bot.runtime.main --dry-run

# Backtest — same profile as final paper comparison
python -m cloud_bot.runtime.main --backtest --days 365 --compare
python -m cloud_bot.runtime.main --backtest --days 1000 --compare

# Start the 24/7 cloud bot loop
python -m cloud_bot.runtime.main --run

# Print the cloud bot status summary
python -m cloud_bot.runtime.main --status
```

From `cloud_bot/` directory:

```bash
cd cloud_bot
python runtime/main.py --backtest --days 365 --compare
python runtime/main.py --backtest --days 1000 --compare
```

Single backtest (no compare table):

```bash
python -m cloud_bot.runtime.main --backtest --days 365
```

## VPS deployment

For a focused deployment guide, see `cloud_bot/deploy/README.md`.

### 1. Sync code from main repo

On the VPS (first time):

```bash
git clone <your-repo-url> /opt/PythonTrading
cd /opt/PythonTrading
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r cloud_bot/requirements.txt
```

**Ongoing sync** (laptop → VPS):

```bash
# On laptop: commit & push
git push origin main

# On VPS:
cd /opt/PythonTrading
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt -q
```

Optional rsync without git:

```bash
rsync -avz --exclude .venv --exclude .git ./PythonTrading/ user@vps:/opt/PythonTrading/
```

### 2. Configure cloud env

```bash
cp cloud_bot/.env.example cloud_bot/.env
# Edit: ALPACA paper keys, CLOUD_BOT_DRY_RUN=false when ready
```

Cloud-specific files (separate from laptop):

| File | Purpose |
|------|---------|
| `cloud_bot/data/cloud_bot_heartbeat.json` | Health |
| `cloud_bot/data/cloud_bot_journal.csv` | Trades |
| `cloud_bot/data/logs/cloud_bot.log` | Rotating logs |

Parent `market_data.db` is shared (same path as laptop repo).

### 3. systemd (24/7)

```bash
sudo cp cloud_bot/deploy/systemd/cloud-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cloud-bot
sudo systemctl start cloud-bot
sudo journalctl -u cloud-bot -f
```

Edit `User=` / `WorkingDirectory=` in the unit file if not using `/opt/PythonTrading`.

### 4. Go live (paper only)

```bash
# In cloud_bot/.env:
CLOUD_BOT_DRY_RUN=false
PAPER_TRADING=true
ALLOW_LIVE_TRADING=false
```

Restart: `sudo systemctl restart cloud-bot`

## Safety

- **Never** set `ALLOW_LIVE_TRADING=true` on cloud without explicit review
- Laptop `run_paper_bot.py` and cloud bot can run **different** heartbeat files
- Heavy features (ML, large universe) → implement in `cloud_bot/modules/` later

## Tests

```bash
python -m pytest cloud_bot/tests/ -q
```
