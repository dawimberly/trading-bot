# Kraken autopilot (live account + wisdom bot)

## Rebalance (main mode)

With `KRAKEN_REBALANCE_ENABLED=true` (default), each cycle builds a **target portfolio** from `reference/kraken_targets.json`:

| Profile | When | Example targets |
|---------|------|-----------------|
| **stress** | Game-plan / macro stress | ~50% USD cash, **VOO** + **SLV**, no crypto adds |
| **calm** | Otherwise | ~25% cash, **VOO**, **NASA**, **SLV**, **WFC**, small **RENDER/ETH** |

The bot sells names **not** in the target (and **GUSH** / banned tickers), trims overweight, buys underweight **crypto via API**, and sends **one Telegram** list for **stock** buys/sells you tap in the app.

Edit weights in `reference/kraken_targets.json`. Update `reference/kraken_positions.json` when your app holdings change (USD estimates for stocks).

---

Automates your **real Kraken Pro** account using the same signals as the Alpaca paper bot:

| Mode | Env | What it does |
|------|-----|----------------|
| **A — Cleanup** | `KRAKEN_AUTOPILOT_CLEANUP=true` | Sells leverage (GUSH), trims duplicate QQQ/VOO, drops smallest names toward 5 positions |
| **B — Crypto mirror** | `KRAKEN_AUTOPILOT_CRYPTO_MIRROR=true` | Same Z-score crypto intents as `run_all.py` (respects crypto vol gate) |
| **C — Paper mirror** | `KRAKEN_AUTOPILOT_MIRROR=true` | Game-plan metal/stress actions + SPY/NYSE buys when session open |

**Wisdom integration:** `WISDOM_MODE=governor` (or `wisdom_pause`) can **pause new buys** on Kraken when the paper bot would pause. **Cleanup sells** still run (risk reduction). Game-plan **stress** and **yield gate** apply the same way as Alpaca.

## Safety (read this)

1. Start with **dry-run** (default): orders are **validated** only, not sent.
2. Turn on live only when ready:

```env
KRAKEN_AUTOPILOT_ENABLED=true
ALLOW_KRAKEN_TRADING=yes
KRAKEN_DRY_RUN=false
KRAKEN_API_KEY=...
KRAKEN_SECRET_KEY=...
```

3. Small account caps:

```env
KRAKEN_MAX_ORDER_USD=25
KRAKEN_CRYPTO_NOTIONAL=15
KRAKEN_CLEANUP_MAX_ACTIONS=2
```

4. **Telegram playbook** (`kraken_send_playbook.py`) is separate — advice only. Autopilot places orders when enabled.

## Commands

```powershell
cd c:\Users\Owner\PythonTrading
.\.venv\Scripts\python.exe scripts\kraken_autopilot_once.py
.\.venv\Scripts\python.exe scripts\kraken_autopilot_once.py --live
python run_all.py
```

`run_all.py` runs autopilot **every cycle** after Alpaca sleeves when `KRAKEN_AUTOPILOT_ENABLED=true`.

Leave **`python run_all.py`** running (or Task Scheduler) so the bot keeps acting — `kraken_autopilot_once.py` is only a single shot.

**What actually auto-executes**

| Action | Auto on Kraken API? |
|--------|---------------------|
| Crypto buys/sells (mirror B) | Yes, when wisdom **not** paused and vol gate open |
| RENDER stress trim (cleanup) | Yes, if order meets Kraken minimum size |
| Stock sells (GUSH, trim small names) | No — one batched Telegram per day |
| SPY/NYSE/metal mirror (C) | Yes only when gates open and pair exists |

## Wisdom modes

| `WISDOM_MODE` | Kraken effect |
|---------------|----------------|
| `governor` | Pauses mirror **buys** when gap wide + stress confirmed |
| `wisdom_pause` | Pauses mirror **buys** when gap wide |
| `arbitrage` / `baseline` | Uses price/regime; no wisdom pause unless regime in panic/bear |

Cleanup (A) is **not** blocked by wisdom pause.

## Stocks vs crypto on Kraken Pro

- **Crypto** (RENDER, ETH, …): autopilot can **place orders** via the Kraken REST API.
- **Stocks tab** (`.EQ` names like `QQQ.EQ`, `GUSH.EQ`): balances show in API but **orders often fail** with “unknown asset pair”. Cleanup (A) then sends a **Telegram** line: “SELL GUSH manually” (once per action per day).

Paper mirror (C) for SPY/NYSE only runs when the pair is API-tradable.

## Pair mapping

Edit `modules/kraken_pairs.py` if a crypto ticker fails (`no Kraken pair`).

Update `reference/kraken_positions.json` when holdings change (used for cleanup USD sorting).

## Disable one mode

```env
KRAKEN_AUTOPILOT_CLEANUP=false
KRAKEN_AUTOPILOT_CRYPTO_MIRROR=false
KRAKEN_AUTOPILOT_MIRROR=false
```

Not financial advice. Test dry-run first.
