# UFC Betting Bot

Standalone UFC value-betting layer — **separate from the crypto trading bot** in this repo. Uses the sibling [`ufc-predictor`](../ufc-predictor/) package for fight features and the trained ensemble model.

## Structure

```
ufc_betting_bot/
├── config/settings.py      # Bankroll rules, paths, odds URLs
├── modules/
│   ├── odds.py             # Historical odds merge (Kaggle + Odds API cache)
│   ├── edge.py             # Implied prob + edge (only when odds exist)
│   ├── bankroll.py         # Fractional Kelly + daily loss limit
│   └── model_bridge.py     # Adapter to ufc-predictor
├── backtester/
│   └── backtest_2025.py    # Event walk-forward backtest
├── live_runner/runner.py   # Dry-run signal generation
├── dashboard/app.py        # Streamlit UI
└── main.py                 # CLI
```

## Quick start

```bash
cd ufc_betting_bot
pip install -r requirements.txt
cp .env.example .env

# Merge odds into ufc-predictor fights + run 2025 backtest
python main.py --backtest-2025 --refresh-odds

# Dashboard
streamlit run dashboard/app.py
```

## Bankroll rules (defaults)

| Rule | Default | Env var |
|------|---------|---------|
| Fractional Kelly | 25% of full Kelly | `UFC_BOT_KELLY_FRACTION=0.25` |
| Max stake per bet | **2%** of bankroll | `UFC_BOT_MAX_BET_FRACTION=0.02` |
| Min stake fraction | 0.5% | `UFC_BOT_MIN_BET_FRACTION=0.005` |
| Daily loss limit | **5%** of day-open bankroll | `UFC_BOT_DAILY_LOSS_LIMIT=0.05` |
| Min edge to bet | 5% | `UFC_BOT_MIN_EDGE=0.05` |

Bets are skipped when odds are missing, edge is below threshold, or the daily loss limit is hit.

## Current performance (2025 walk-forward backtest)

Last run on **2025 UFC events** (362 fights scored, 42 events):

| Metric | Value |
|--------|-------|
| Model accuracy | 43.4% |
| ROC AUC | 0.543 |
| Fights with closing odds | 329 / 362 |
| Flat-stake ROI (edge ≥ 5%, legacy) | +76.8% on $1k |
| **Fractional Kelly** (2% cap, 5% min edge) | **+139.8% ROI**, 266 trades, 44.4% hit rate on $1k |

> **Caveat:** High flat-stake ROI reflects many small-edge bets with uncapped stakes. The betting bot uses capped fractional Kelly and daily stop-loss for live use. Model calibration still needs work before real money.

Odds coverage: **Ultimate UFC dataset** (Kaggle mirror) + jansen88 historical lines, joined on event + date + fighter names.

## CLI

```bash
python main.py merge-odds              # Merge odds into ufc-predictor/data/raw/fights.csv
python main.py backtest-2025           # Walk-forward backtest
python main.py backtest-2025 --year 2024
python main.py live                    # Dry-run signals for next card
```

## Next milestones

1. **Calibration** — isotonic / conformal adjustment before edge calc (reduce overconfidence)
2. **Closing vs open odds** — separate backtest modes; avoid training on closing line if claiming early predictions
3. **Live execution** — DraftKings / Odds API polling + bet slip export (no auto-bet yet)
4. **Per-bookmaker line shop** — best available price instead of averaged odds
5. **Alerting** — Telegram/discord when edge > threshold on upcoming main events
6. **Paper ledger** — track hypothetical vs actual fills with CLV (closing line value)

## Tests

```bash
cd ufc_betting_bot
python -m pytest tests/ -v
```
