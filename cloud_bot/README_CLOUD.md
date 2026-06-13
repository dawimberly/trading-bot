# Cloud Bot — VPS Deployment Guide

Production entry point for **Best Paper Bot v2.1** on a $30–50/month Ubuntu VPS. The cloud bot is a thin **supervisor** around parent-repo `run_all.py` — no duplicate strategy code.

## Production entry point

All modes go through `cloud_bot/runtime/main.py`:

| Flag | Purpose |
|------|---------|
| `--run` | Start 24/7 supervisor (spawns `run_all.py`, restarts with backoff) |
| `--backtest` | Run backtest (`--days`, `--max`, `--refresh`, `--compare`) |
| `--status` | Health summary (supervisor PID, heartbeat age, equity) |
| `--stop` | SIGTERM supervisor, remove PID file |
| `--dry-run` | Validate config + Alpaca keys; do not trade |

```bash
cd cloud_bot

python runtime/main.py --dry-run      # validate before go-live
python runtime/main.py --backtest --days 365 --compare
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

### Locked OFF (enforced in `config/profile.py`)

Macro regime, risk parity, stat arb optimized, social/Felix sleeve, equity pairs, SPY MA exit.

**Forced safety (cannot override via host `.env`):**

- `PAPER_TRADING=true`
- `ALLOW_LIVE_TRADING=false`
- Paper REST endpoint: `https://paper-api.alpaca.markets`

Thinking engine: off by default on cloud; opt-in via `PAPER_THINKING_ENGINE_ENABLED=true` in `cloud_bot/.env`.

---

## Quickstart (Ubuntu 22.04 / 24.04)

```bash
# 1. System packages
sudo apt update && sudo apt install -y git python3 python3-venv python3-pip

# 2. Deploy user + clone
sudo useradd -m -s /bin/bash trader || true
sudo mkdir -p /opt/PythonTrading
sudo chown trader:trader /opt/PythonTrading
sudo -u trader git clone <your-repo-url> /opt/PythonTrading
cd /opt/PythonTrading

# 3. Python env
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -r cloud_bot/requirements.txt

# 4. Configure secrets (paper keys only)
cp cloud_bot/.env.example cloud_bot/.env
nano cloud_bot/.env   # APCA_* + CLOUD_BOT_DRY_RUN=true initially

# 5. Validate
cd cloud_bot
python runtime/main.py --dry-run
python runtime/main.py --backtest --days 365 --compare
python runtime/main.py --status

# 6. Go live (paper)
# Set CLOUD_BOT_DRY_RUN=false in cloud_bot/.env, then:
python runtime/main.py --run
# Or install systemd unit (section below)
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
