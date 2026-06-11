# Cloud Bot Deployment Guide

This file covers a minimal Ubuntu VPS deployment for the `cloud_bot` package.
It is designed to run the best paper bot stack 24/7 on a $30–50/month VPS.

## Assumptions

- Repo is deployed to `/opt/PythonTrading`
- Python 3.11+ is installed
- A virtual environment is used at `/opt/PythonTrading/.venv`
- `cloud_bot/.env` is configured for cloud/paper trading

## 1. Install OS dependencies

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

## 2. Clone or sync repository

```bash
sudo mkdir -p /opt/PythonTrading
sudo chown "$USER":"$USER" /opt/PythonTrading
cd /opt/PythonTrading
git clone <your-repo-url> .
```

## 3. Create the Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -r cloud_bot/requirements.txt
```

## 4. Configure cloud bot environment

```bash
cd cloud_bot
cp .env.example .env
```

Edit `cloud_bot/.env` and set at minimum:

- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `CLOUD_BOT_DRY_RUN=false` when you are ready to run
- `PAPER_TRADING=true`
- `ALLOW_LIVE_TRADING=false`

Optional values:

- `CLOUD_BOT_PROFILE=paper_aggressive`
- `CLOUD_BOT_CYCLE_SEC=45`
- `CLOUD_BOT_RESTART_SEC=30`
- `HEARTBEAT_FILE=cloud_bot/data/cloud_bot_heartbeat.json`
- `PAPER_JOURNAL_CSV=cloud_bot/data/cloud_bot_journal.csv`

## 5. Verify the cloud bot locally

From `/opt/PythonTrading/cloud_bot`:

```bash
source /opt/PythonTrading/.venv/bin/activate
python runtime/main.py --status
python runtime/main.py --dry-run
python runtime/main.py --backtest --days 365
```

## 6. Install the systemd service

```bash
sudo cp cloud_bot/deploy/systemd/cloud-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cloud-bot
sudo systemctl start cloud-bot
sudo journalctl -u cloud-bot -f
```

If you changed the repository path or user, update `/etc/systemd/system/cloud-bot.service` accordingly.

## 7. Recommended runtime checks

- `systemctl status cloud-bot`
- `sudo journalctl -u cloud-bot -n 100 --no-pager`
- `ls -l cloud_bot/data`
- `tail -n 50 cloud_bot/data/logs/cloud_bot.log`

## 8. Notes for a $30–50/month VPS

- Use a lightweight Ubuntu image (22.04 or 24.04 LTS)
- Keep the repository on SSD storage
- Avoid running heavy training or data downloads on the same host
- Keep `ALLOW_LIVE_TRADING=false` unless you explicitly want live execution
- Use separate cloud-specific Alpaca keys and `cloud_bot/.env`

## 9. Updating code on the VPS

```bash
cd /opt/PythonTrading
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt -q
sudo systemctl restart cloud-bot
```
