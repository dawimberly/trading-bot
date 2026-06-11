# Cloud Bot — VPS Deployment Guide

Production entry point for the **Best Paper Bot** stack on a $30–50/month Ubuntu VPS. The cloud bot is a thin supervisor around parent-repo `run_all.py` — no duplicate strategy code.

## Stack (default ON)

| Feature | Env flag |
|---------|----------|
| Dynamic VTI (40–75%) | `PAPER_DYNAMIC_VTI=true` |
| Dynamic risk (1–3%) | `PAPER_DYNAMIC_RISK_ENABLED=true` |
| Statistical arbitrage | `PAPER_STAT_ARB_ENABLED=true` |
| Volatility overlay (log-only live) | `PAPER_VOL_TRADING_ENABLED=true` |
| Options income | `PAPER_OPTIONS_SLEEVE_ENABLED=true` |
| Macro regime adaptor | `PAPER_MACRO_REGIME_ADAPTOR_ENABLED=true` |
| NYSE overlap / adaptive chunk / co-fire | `PAPER_NYSE_*`, `PAPER_ADAPTIVE_*`, `PAPER_COFIRE_*` |
| SPY exit on MA break | `PAPER_SPY_EXIT_ON_MA_BREAK=true` |

Forced safety: `PAPER_TRADING=true`, `ALLOW_LIVE_TRADING=false` (cannot be overridden by host `.env`).

---

## Commands

Run from `cloud_bot/` or repo root (`python -m cloud_bot.runtime.main`).

```bash
cd cloud_bot

# Validate config (no trading)
python runtime/main.py --dry-run

# Backtest — compare vs legacy + live vol parity + VTI
python runtime/main.py --backtest --days 365 --compare

# Production 24/7 loop (supervises run_all.py)
python runtime/main.py --run

# Health check
python runtime/main.py --status

# Graceful stop
python runtime/main.py --stop
```

---

## 1. VPS sizing ($30–50/mo)

| Provider tier | Spec | Notes |
|---------------|------|-------|
| Hetzner CX22 / DO Basic | 2 vCPU, 4 GB RAM | Comfortable for paper bot + SQLite |
| Minimum | 1 vCPU, 2 GB RAM | OK with `DB_MATRIX_CACHE_SEC=180` |

Disk: 20 GB+ (repo + `market_data.db` + logs).

---

## 2. Initial setup (Ubuntu 22.04/24.04)

```bash
sudo apt update && sudo apt install -y git python3 python3-venv python3-pip

sudo useradd -m -s /bin/bash trader || true
sudo usermod -aG sudo trader   # optional

sudo mkdir -p /opt/PythonTrading
sudo chown trader:trader /opt/PythonTrading
sudo -u trader git clone <your-repo-url> /opt/PythonTrading
cd /opt/PythonTrading

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -r cloud_bot/requirements.txt
```

---

## 3. Configure environment

```bash
cp cloud_bot/.env.example cloud_bot/.env
nano cloud_bot/.env
```

**Required:**

```ini
PAPER_TRADING=true
ALLOW_LIVE_TRADING=false
CLOUD_BOT_DRY_RUN=false          # true until keys verified
CLOUD_BOT_PROFILE=paper_aggressive

APCA_API_KEY_ID=your_paper_key
APCA_API_SECRET_KEY=your_paper_secret
```

Optional parent `.env` at repo root for shared keys; `cloud_bot/.env` overrides.

**Cloud-isolated paths** (auto-set by `runtime/main.py`):

| File | Purpose |
|------|---------|
| `cloud_bot/data/cloud_bot_heartbeat.json` | Last cycle health |
| `cloud_bot/data/cloud_bot_journal.csv` | Trade journal |
| `cloud_bot/data/stat_arb_open_book.json` | Pair book persistence |
| `cloud_bot/data/logs/cloud_bot.log` | Supervisor log |
| `cloud_bot/data/cloud_bot.pid` | Loop PID for `--stop` |

Shared: `market_data.db` at repo root (same as laptop).

---

## 4. Pre-flight checks

```bash
source /opt/PythonTrading/.venv/bin/activate
cd /opt/PythonTrading/cloud_bot

python runtime/main.py --dry-run
python runtime/main.py --backtest --days 365 --compare
python runtime/main.py --status
```

Set `CLOUD_BOT_DRY_RUN=false` when ready.

---

## 5. systemd (24/7)

```bash
sudo cp /opt/PythonTrading/cloud_bot/deploy/systemd/cloud-bot.service /etc/systemd/system/
```

Edit the unit if paths differ:

```ini
User=trader
WorkingDirectory=/opt/PythonTrading
EnvironmentFile=-/opt/PythonTrading/cloud_bot/.env
ExecStart=/opt/PythonTrading/.venv/bin/python -m cloud_bot.runtime.main --run
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable cloud-bot
sudo systemctl start cloud-bot
sudo systemctl status cloud-bot
```

Logs:

```bash
sudo journalctl -u cloud-bot -f
tail -f /opt/PythonTrading/cloud_bot/data/logs/cloud_bot.log
```

Stop without systemd:

```bash
python runtime/main.py --stop
# or
sudo systemctl stop cloud-bot
```

---

## 6. Log rotation

Install logrotate snippet:

```bash
sudo cp /opt/PythonTrading/cloud_bot/deploy/logrotate/cloud-bot /etc/logrotate.d/cloud-bot
```

Default: rotate `cloud_bot.log` daily, keep 14 days, compress.

Journald retention (optional):

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
echo -e "[Journal]\nSystemMaxUse=200M" | sudo tee /etc/systemd/journald.conf.d/size.conf
sudo systemctl restart systemd-journald
```

---

## 7. Monitoring

### Heartbeat

```bash
python runtime/main.py --status
cat data/cloud_bot_heartbeat.json | python -m json.tool
```

Alert if `heartbeat_age` > 600s (stale bot).

### Key fields

- `equity`, `cash`, `regime`, `halted`
- `sleeve_exposure` — active sleeves
- `dynamic_vol_score`, `sleeve_caps.vti_core`

### Optional Telegram / email

Set `TELEGRAM_*` or `SMTP_*` in `cloud_bot/.env` (inherited by `run_all.py`).

### Cron health ping (example)

```cron
*/15 * * * * cd /opt/PythonTrading/cloud_bot && /opt/PythonTrading/.venv/bin/python runtime/main.py --status | mail -s "cloud-bot" you@example.com
```

---

## 8. Restart behavior

The supervisor (`runtime/loop.py`) wraps `run_all.py`:

| Setting | Default | Behavior |
|---------|---------|----------|
| `CLOUD_BOT_RESTART_SEC` | 30 | Base delay between restarts |
| `CLOUD_BOT_MAX_RESTARTS` | 20 | Exit after consecutive failures (systemd restarts unit) |

Exponential backoff: 30s → 60s → … up to 600s.

Graceful shutdown: `SIGTERM` / `SIGINT` → terminate child → remove PID file.

---

## 9. Sync laptop → VPS

```bash
# Laptop
git add -A && git commit -m "..." && git push

# VPS
cd /opt/PythonTrading && git pull
source .venv/bin/activate
pip install -r requirements.txt -q
sudo systemctl restart cloud-bot
```

Rsync alternative:

```bash
rsync -avz --exclude .venv --exclude .git ./PythonTrading/ trader@vps:/opt/PythonTrading/
```

---

## 10. Safety checklist

- [ ] `ALLOW_LIVE_TRADING` stays `false` on cloud
- [ ] Dedicated **paper** Alpaca keys (not live account)
- [ ] `cloud_bot/.env` never committed
- [ ] Laptop and cloud use **different** heartbeat paths
- [ ] Backtest `--compare` run after profile changes
- [ ] Vol overlay: live is log-only; compare table includes **live vol parity** row

---

## 11. Troubleshooting

| Symptom | Action |
|---------|--------|
| `running: no` | `CLOUD_BOT_DRY_RUN=false`, check systemd |
| `run_all.py exited code=1` | Check `cloud_bot.log`, Alpaca keys, `market_data.db` |
| Stale heartbeat | `systemctl restart cloud-bot`; verify network |
| Max restarts exceeded | Fix root cause; `systemctl reset-failed cloud-bot` |
| High CPU | Raise `DB_MATRIX_CACHE_SEC=300` in `.env` |

---

## Layout

```
cloud_bot/
├── config/
│   ├── env_loader.py    # .env load order + profile merge
│   ├── profile.py       # Best paper stack defaults
│   └── settings.py      # Paths, PID, heartbeat
├── runtime/
│   ├── main.py          # --run | --backtest | --status | --stop
│   ├── loop.py          # Supervisor + backoff
│   └── backtest.py
├── deploy/
│   ├── systemd/cloud-bot.service
│   └── logrotate/cloud-bot
├── data/                # Runtime (gitignored)
└── .env.example
```

See also: [`README.md`](README.md) (quick start), [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
