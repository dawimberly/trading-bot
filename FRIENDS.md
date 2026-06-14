# Friends: run the bots from GitHub

Share one repo — each person runs on **their own PC** with **their own keys**. Nothing is hosted for you; clone, install, run locally.

**Repo:** [github.com/dawimberly/trading-bot](https://github.com/dawimberly/trading-bot)

---

## Stock trading bot (Alpaca)

Paper trading first. No manual `.env` editing — use the browser portal.

### Windows

1. Install [Python 3.11+](https://www.python.org/downloads/) (check **Add python.exe to PATH**).
2. Clone and enter the stock bot folder:
   ```powershell
   git clone https://github.com/dawimberly/trading-bot.git
   cd trading-bot\stock-bot
   ```
3. Double-click **`friend_setup.bat`** — creates `.venv`, installs deps, opens the portal.
4. In the portal:
   - **Register** (account stays on their machine)
   - **Connect Alpaca** — [paper API keys](https://app.alpaca.markets/paper/dashboard/overview)
   - **Bot** tab → **Download market data** (once) → **Start bot**

### Mac / Linux

```bash
git clone https://github.com/dawimberly/trading-bot.git
cd trading-bot/stock-bot
chmod +x friend_setup.sh
./friend_setup.sh
```

| File | Purpose |
|------|---------|
| `stock-bot/friend_setup.bat` / `.sh` | Install + open portal |
| `stock-bot/portal.py` | Login, Alpaca keys, dashboard, bot control |

Root `friend_setup.bat` forwards into `stock-bot/` if they clone to the repo root.

---

## UFC betting bot (predictor + dashboard)

Separate from the stock bot. Uses sibling folders `ufc-predictor/` (model) and `ufc_betting_bot/` (odds, Kelly sizing, dashboard).

**Paper / dry-run only** — no auto-betting. Optional [The Odds API](https://the-odds-api.com) key for live lines.

### Windows

1. Python 3.11+ installed (same as above).
2. Clone the repo (if not already):
   ```powershell
   git clone https://github.com/dawimberly/trading-bot.git
   cd trading-bot\ufc_betting_bot
   ```
3. Double-click **`friend_setup.bat`**.
   - First run downloads fight data and trains the model (**~15–30 min**, one time).
   - Browser opens the UFC dashboard on port **8502**.
4. Optional: edit `ufc_betting_bot\.env` and set `THE_ODDS_API_KEY=your_key` for live odds.

### Mac / Linux

```bash
git clone https://github.com/dawimberly/trading-bot.git
cd trading-bot/ufc_betting_bot
chmod +x friend_setup.sh
./friend_setup.sh
```

### Manual CLI (after setup)

```powershell
cd trading-bot
.\ufc_betting_bot\.venv\Scripts\Activate.ps1
set PYTHONPATH=%CD%
python ufc_betting_bot\main.py --backtest-2025
streamlit run ufc_betting_bot\dashboard\app.py --server.port 8502
```

| File | Purpose |
|------|---------|
| `ufc_betting_bot/friend_setup.bat` / `friend_setup.sh` | Install both packages + bootstrap model |
| `ufc_betting_bot/dashboard/app.py` | Backtest summary, dry-run signals |
| `ufc-predictor/main.py` | Data refresh, train, predictions |

---

## What stays private

Never commit or share:

- `.env` files (API keys)
- `stock-bot/data/portal/users/` (portal accounts)
- Alpaca live keys until they explicitly opt in

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `python` not found | Reinstall Python with PATH checked, or use `py -3.11` |
| Stock portal won't start | Port 8501 in use — close other Streamlit apps |
| UFC "No trained model" | Re-run `ufc_betting_bot/friend_setup.bat` or `python ufc-predictor/main.py --refresh-data --train` |
| UFC dashboard empty | Run backtest once: `python ufc_betting_bot/main.py --backtest-2025` |
