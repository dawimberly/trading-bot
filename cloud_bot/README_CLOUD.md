# Cloud Bot — VPS Deployment Guide

Production entry point for **Best Paper Bot v2.1** on a $30–50/month Ubuntu VPS. The cloud bot is a thin **supervisor** around parent-repo `run_all.py` — no duplicate strategy code.

---

## Choose a VPS provider (beginner-friendly)

Both **DigitalOcean** and **Hetzner** work well for a small Python trading bot. Pick one:

| Provider | Typical plan | Notes |
|----------|--------------|-------|
| [DigitalOcean](https://www.digitalocean.com/) | Basic Droplet, 2 GB RAM, Ubuntu 24.04 | Simple UI, good docs, ~$12–18/mo |
| [Hetzner](https://www.hetzner.com/cloud) | CX22 (2 vCPU, 4 GB), Ubuntu 24.04 | Strong price/performance in EU/US |

**Steps (same for both):**

1. Create an account and add SSH key (or use password login initially).
2. Create a **Ubuntu 24.04 LTS** server in a region close to you (or US-East for Alpaca latency).
3. Note the **public IP** — you will SSH as `root@YOUR_IP`.
4. Optional: point a domain at the IP (not required for the bot).

```bash
# From your laptop (replace YOUR_IP)
ssh root@YOUR_IP

# Create a non-root user (recommended)
adduser trader
usermod -aG sudo trader
rsync --archive --chown=trader:trader ~/.ssh /home/trader
# Log in as trader from now on:
ssh trader@YOUR_IP
```

Firewall (allow SSH only from your IP if possible):

```bash
sudo ufw allow OpenSSH
sudo ufw enable
```

---

## Production entry point

All modes go through `cloud_bot/runtime/main.py`:

| Flag | Purpose |
|------|---------|
| `--run` | Start 24/7 supervisor (spawns `run_all.py`, restarts with backoff) |
| `--backtest` | Run backtest (`--days`, `--max`, `--refresh`, `--compare`, `--fast-mode`) |
| `--status` | Health summary (supervisor PID, heartbeat age, equity) |
| `--stop` | SIGTERM supervisor, remove PID file |
| `--dry-run` | Validate config + Alpaca keys; do not trade |

```bash
cd cloud_bot

python runtime/main.py --dry-run      # validate before go-live
python runtime/main.py --backtest --days 365 --compare
python runtime/main.py --backtest --days 365 --fast-mode   # quick validation
python runtime/main.py --run          # foreground 24/7 loop
python runtime/main.py --status       # health check
python runtime/main.py --stop         # graceful stop
```

Systemd (recommended on VPS):

```bash
sudo systemctl start cloud-bot    # runs: python -m cloud_bot.runtime.main --run
python runtime/main.py --status
```

---

## Stack (Best Paper Bot v2.1)

### ON (default)

| Feature | Env flag |
|---------|----------|
| Dynamic VTI (40–75%) | `PAPER_DYNAMIC_VTI=true` |
| Dynamic risk (1–3%) | `PAPER_DYNAMIC_RISK_ENABLED=true` |
| Statistical arbitrage | `PAPER_STAT_ARB_ENABLED=true` |
| Vol overlay (log-only PnL on cloud) | `PAPER_VOL_TRADING_ENABLED=true` |
| Options income | `PAPER_OPTIONS_SLEEVE_ENABLED=true` |
| NYSE overlap / chunk / co-fire | `PAPER_NYSE_*`, `PAPER_ADAPTIVE_*`, `PAPER_COFIRE_*` |
| Dynamic universe | `PAPER_DYNAMIC_UNIVERSE=true` |
| Thinking engine (opt-in) | `PAPER_THINKING_ENGINE_ENABLED=true` |

### Safety guards (always on)

- Paper only: `PAPER_TRADING=true`, `ALLOW_LIVE_TRADING=false` (forced in code)
- Daily loss circuit breaker (4% paper)
- Thinking tilt cap ±6% per sleeve when engine enabled
- Live trading keys rejected on cloud profile

### Locked OFF (enforced in `config/profile.py`)

Macro regime, risk parity, stat arb optimized, social/Felix sleeve, equity pairs, SPY MA exit.

**Forced safety (cannot override via host `.env`):**

- `PAPER_TRADING=true`
- `ALLOW_LIVE_TRADING=false`
- Paper REST endpoint: `https://paper-api.alpaca.markets`

Thinking engine: off by default on cloud; opt-in via `PAPER_THINKING_ENGINE_ENABLED=true` in `cloud_bot/.env`.

---

## Quickstart (Ubuntu 22.04 / 24.04)

### First-time VPS setup

```bash
# 1. System packages
sudo apt update && sudo apt upgrade -y
sudo apt install -y git python3 python3-venv python3-pip

# 2. App directory (as trader user)
sudo mkdir -p /opt/PythonTrading
sudo chown trader:trader /opt/PythonTrading
cd /opt/PythonTrading

# 3. Clone your repo (replace URL)
git clone https://github.com/YOUR_USER/PythonTrading.git .
# Or: git pull if already cloned

# 4. Python env
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -r cloud_bot/requirements.txt

# 5. Configure secrets (paper keys only)
cp cloud_bot/.env.example cloud_bot/.env
nano cloud_bot/.env   # APCA_* + CLOUD_BOT_DRY_RUN=true initially

# 6. Validate
cd cloud_bot
python runtime/main.py --dry-run
python runtime/main.py --status
python runtime/main.py --backtest --days 365 --fast-mode
python runtime/main.py --backtest --days 365 --compare   # full compare (slower)

# 7. Go live (paper)
# Set CLOUD_BOT_DRY_RUN=false in cloud_bot/.env, then:
python runtime/main.py --run
# Or install systemd unit (section below)
```

### Updating after `git pull` (routine deploy)

```bash
cd /opt/PythonTrading
git pull
source .venv/bin/activate
pip install -r requirements.txt -q
pip install -r cloud_bot/requirements.txt -q
sudo systemctl restart cloud-bot
cd cloud_bot && python runtime/main.py --status
```

---

## Environment files

Load order (`config/env_loader.py`):

1. Repo root `.env` — shared keys (optional on VPS)
2. `cloud_bot/.env` — **cloud overrides** (required on VPS)

**Required in `cloud_bot/.env`:**

```ini
PAPER_TRADING=true
ALLOW_LIVE_TRADING=false
CLOUD_BOT_DRY_RUN=false
CLOUD_BOT_PROFILE=paper_aggressive

APCA_API_KEY_ID=your_paper_key_id
APCA_API_SECRET_KEY=your_paper_secret_key
```

**Supervisor tuning:**

| Variable | Default | Description |
|----------|---------|-------------|
| `CLOUD_BOT_RESTART_SEC` | 30 | Base delay between restarts |
| `CLOUD_BOT_MAX_RESTARTS` | 20 | Stop supervisor after N consecutive failures |
| `CLOUD_BOT_CYCLE_SEC` | 45 | Passed to parent bot sleep hint |
| `DB_MATRIX_CACHE_SEC` | 120 | Reduce SQLite load on small VPS |

---

## Runtime paths

| Path | Purpose |
|------|---------|
| `cloud_bot/data/cloud_bot_heartbeat.json` | Last trading cycle health |
| `cloud_bot/data/cloud_bot_journal.csv` | Trade journal |
| `cloud_bot/data/stat_arb_open_book.json` | Pair book persistence |
| `cloud_bot/data/logs/cloud_bot.log` | Supervisor log (daily rotation, 14 days) |
| `cloud_bot/data/logs/run_all_subprocess.log` | Child `run_all.py` stdout/stderr |
| `cloud_bot/data/cloud_bot.pid` | Supervisor PID for `--stop` |
| `logs/run_all.log` | Parent bot log (repo root, daily rotation) |
| `market_data.db` | Shared SQLite OHLCV (repo root) |

---

## systemd (24/7 production)

```bash
sudo cp /opt/PythonTrading/cloud_bot/deploy/systemd/cloud-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cloud-bot
sudo systemctl start cloud-bot
sudo systemctl status cloud-bot
```

Unit runs:

```ini
ExecStart=/opt/PythonTrading/.venv/bin/python -m cloud_bot.runtime.main --run
EnvironmentFile=-/opt/PythonTrading/cloud_bot/.env
WorkingDirectory=/opt/PythonTrading
```

**Logs:**

```bash
sudo journalctl -u cloud-bot -f
tail -f /opt/PythonTrading/cloud_bot/data/logs/cloud_bot.log
tail -f /opt/PythonTrading/logs/run_all.log
```

**Stop:**

```bash
python runtime/main.py --stop
# or
sudo systemctl stop cloud-bot
```

---

## Supervisor behavior (`runtime/loop.py`)

- Spawns `run_all.py` with Best Paper profile env merged in
- **Exponential backoff** on failures: 30s → 60s → … capped at 600s (+ jitter)
- **Max restarts:** `CLOUD_BOT_MAX_RESTARTS` (default 20) then exit (systemd `Restart=always` relaunches unit)
- **Graceful shutdown:** SIGTERM/SIGINT → terminate child → remove PID file
- **Structured events:** grep `event=` in `cloud_bot.log`
- **Single instance:** refuses `--run` if PID file points to live process

---

## Monitoring

```bash
cd /opt/PythonTrading/cloud_bot
python runtime/main.py --status
cat data/cloud_bot_heartbeat.json | python -m json.tool
```

Alert if `heartbeat_age` > **600s** (stale bot).

Key heartbeat fields: `equity`, `cash`, `regime`, `halted`, `sleeve_exposure`, `sleeve_caps`.

Optional cron (every 15 min):

```cron
*/15 * * * * cd /opt/PythonTrading/cloud_bot && /opt/PythonTrading/.venv/bin/python runtime/main.py --status >> /tmp/cloud-bot-status.log 2>&1
```

Telegram/email: set `TELEGRAM_*` or `SMTP_*` in `.env` (inherited by `run_all.py`).

---

## Log rotation

| Log | Rotation |
|-----|----------|
| `cloud_bot/data/logs/cloud_bot.log` | Daily, 14 days (built-in `TimedRotatingFileHandler`) |
| `logs/run_all.log` | Daily, 7 days (parent `logging_utils.setup_logging`) |
| `logs/events.log` | Daily, 7 days (structured `log_event` output) |

Optional system logrotate:

```bash
sudo cp /opt/PythonTrading/cloud_bot/deploy/logrotate/cloud-bot /etc/logrotate.d/cloud-bot
```

Journald size cap (optional):

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
echo -e "[Journal]\nSystemMaxUse=200M" | sudo tee /etc/systemd/journald.conf.d/size.conf
sudo systemctl restart systemd-journald
```

---

## Deploy updates (laptop → VPS)

```bash
# Laptop
git push

# VPS
cd /opt/PythonTrading && git pull
source .venv/bin/activate
pip install -r requirements.txt -q
pip install -r cloud_bot/requirements.txt -q
sudo systemctl restart cloud-bot
python cloud_bot/runtime/main.py --status
```

---

## Safety checklist

- [ ] Dedicated **paper** Alpaca keys on cloud (not live account)
- [ ] `ALLOW_LIVE_TRADING=false` on VPS
- [ ] `cloud_bot/.env` never committed
- [ ] Different heartbeat path than laptop (`cloud_bot_heartbeat.json` vs `bot_heartbeat.json`)
- [ ] Run `--backtest --compare` after profile changes
- [ ] Vol overlay: synthetic in backtest; live/cloud logs only

---

## Troubleshooting

| Symptom | Action |
|---------|--------|
| `supervisor: no` | Set `CLOUD_BOT_DRY_RUN=false`; start with `--run` or systemd |
| `Cloud bot already running` | `python runtime/main.py --stop` |
| `run_all.py exited code=1` | Check `run_all_subprocess.log`, Alpaca keys, `market_data.db` |
| Stale heartbeat (>600s) | `systemctl restart cloud-bot`; check network |
| Max restarts exceeded | Fix root cause; `systemctl reset-failed cloud-bot` |
| High CPU / disk I/O | Raise `DB_MATRIX_CACHE_SEC=300` |

---

## Layout

```
cloud_bot/
├── config/
│   ├── env_loader.py      # .env load order + runtime env merge
│   ├── profile.py         # Best Paper v2.1 defaults + forced safety
│   └── settings.py        # Paths, PID, heartbeat
├── runtime/
│   ├── main.py            # --run | --backtest | --status | --stop | --dry-run
│   ├── loop.py            # Supervisor + exponential backoff
│   ├── logging_setup.py   # Daily cloud_bot.log rotation
│   └── backtest.py
├── deploy/
│   ├── systemd/cloud-bot.service
│   └── logrotate/cloud-bot
├── data/                  # Runtime (gitignored)
└── .env.example
```

See also: main repo [`README.md`](../README.md) (Profile B), [`scripts/analysis/final_paper_bot_backtest.md`](../scripts/analysis/final_paper_bot_backtest.md).
