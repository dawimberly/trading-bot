# Kraken full automation (run_all)

## What run_all does automatically

| Asset type | API trading | Notes |
|------------|-------------|--------|
| **Crypto** (RENDER, ETH, …) | **Yes** | Real market orders each cycle |
| **xStocks** (VOOxUSD, SLVxUSD, …) | Yes if API key has **tokenized/xStocks** permission |
| **Pro stocks tab (.EQ)** | **No** | GUSH.EQ, NASA.EQ, VOO.EQ are a different product — Kraken REST cannot sell them |

Your probe (`python scripts/account/kraken_capabilities_check.py`) shows what your key can do.

## Make stocks automatic (one-time setup)

1. Kraken → **API** → edit your key → enable **tokenized assets / xStocks** trading.
2. Kraken Pro → **Account** → unlock **xStocks** (if eligible in your region).
3. In the app, **sell .EQ positions** and **buy xStock equivalents** (e.g. VOOx) once — after that the bot can trade those via API.

If you see `US:TX restricted` on equity pairs, Kraken blocks stock API in your region — only **crypto** will auto-rebalance.

## Run it

```powershell
cd c:\Users\Owner\PythonTrading
.\.venv\Scripts\python.exe run_all.py
```

Requires in `.env`:

```env
KRAKEN_AUTOPILOT_ENABLED=true
KRAKEN_REBALANCE_ENABLED=true
ALLOW_KRAKEN_TRADING=yes
KRAKEN_DRY_RUN=false
```

No more manual Telegram lists for cleanup (`KRAKEN_NO_MANUAL_ALERTS=true` default).

Edit targets: `reference/kraken_targets.json`
