# 12-to-7-month momentum vs current NYSE ranking

**Decision: inconclusive.** The required simulation was not run. No fold returns, Sharpe, drawdown, turnover, trade count, or cost totals are reported. This screen does not support a forward paper shadow test and does not support promotion.

Production settings were not changed. No live or paper bot was started or restarted. The final holdout was not touched, because no walk-forward was built. Files under `sentiment/sources/` were not modified.

## Launch path

The repo has two enabled books, `alpaca_live` and `alpaca_paper`. There is no Medium book.

| Book | Process |
|------|---------|
| Daily start | `Start_Bot_and_Dashboard.bat` → `scripts/owner_reset.py` |
| Live | `run_all.py` (Profile A) |
| Paper | `run_paper_bot.py` sets `PAPER_CHASE_MODE=1` and starts `run_all.py` |

NYSE orders for both books go through `run_all.py` → `run_nyse_momentum_and_stat_arb` → `run_equity_strategy` → `executor.execute_order(..., sleeve="NYSE")`.

On paper startup, `init_paper_chase_if_enabled()` tries `config.best_paper_config`. `config.py` shadows the `config/` directory, so that import fails (`config` is not a package) and the code falls back to `enforce_best_paper_stack()` → `enforce_realistic_research_profile()`. The coverage script uses that same fallback and does not call `maybe_refresh_screener_universe()` (that refresh can hit the network).

## What the order code actually ranks

`_equity_momentum_candidates` ranks by `(last close / moving average) - 1`. The window is hardcoded: 50 bars, or 20 for an IPO. A name is eligible when the last close is above that average times `1 - NYSE_MA_ENTRY_TOLERANCE_PCT` (0.01).

`PAPER_NYSE_MA_WINDOW` is 70, and `effective_nyse_ma_window()` returns 70 once the paper-aggressive context is on. That helper is used for the startup banner in `run_all.py`. The candidate function does not call it, so the score that reaches `execute_order` is the 50-bar ratio.

Optional add-ons (insider, RVOL, ORB, catalyst, multi-timeframe, 12:00–14:00 ET boost) are applied only when their own flags are on. They were left in place for this design; the candidate test was only going to replace the base ratio.

## Rules checked against the paper launch path

Values below are from `scripts/research/nyse_12_7_momentum_coverage.py` after the paper fallback above. No portal `.env` is in the repo (`data/portal/users/` is gitignored), so these are the committed defaults.

| Rule the screen was asked to match | Active code |
|------------------------------------|-------------|
| 15-name book | No 15-name position cap. Paper allows 3 new NYSE entries per cycle (`effective_max_equity_trades`, hard ceiling 5). Live, without paper chase, allows 1. Portfolio cap is 25 non-core names, 8% per name, NYSE sleeve 20% of equity with a 22% high-cash ceiling. The screener’s `SECTOR_CAP = 15` limits names per GICS sector inside a top-75 list. The checked-in `data/screener_universe.json` has 31 tickers and zeroed momentum and price fields. Fixed universe size is 30. |
| 30-day hold | `PAPER_POSITION_MAX_HOLD_BARS` is 30, used when exit optimization is off. Paper exit optimization is on, so the time exit that fires is `EXIT_OPTIMIZATION_MAX_HOLD_BARS` = 35. Age is wall-clock days from the position timestamp, not a count of trading sessions. |
| 2× daily ATR stop | Initial multiplier is 2.0 (`ATR_STOP_MULTIPLIER` / `ATR_RISK_MULTIPLE`). `calculate_atr` is the 14-day mean of absolute close-to-close changes, not high-low true range. Paper smart stops are on: reevaluation at −5% (tighten to 1× ATR or cut size 50%) and a hard exit at −10%. ATR sizing is on and caps notional (`ATR_MAX_SIZE_PCT` default 4%, risk per trade 1.8% on the paper lock). |
| 9:30–10:15 ET cooldown | Equity orders start `EQUITY_SCAN_AFTER_OPEN_MIN` minutes after the open. That default is 5, so 9:35 ET. The quality-fix cooldown in source is `time(9, 30) <= t <= time(10, 0)`. `PAPER_MOMENTUM_QUALITY_FIXES` is false, and the paper enforcer does not turn it on, so that cooldown does not run. `.env.example` also sets it false. |
| 2.0% overnight gap skip | Implemented as `gap > 0.02`, inside the same quality-fix gate, so it is off. When on, live uses yfinance open vs prior close; backtests use close vs prior close. |
| 5 bps slippage | Research default in `modules/backtester_core.py` is 5 bps equity slippage and 0 bps commission (`BACKTEST_EQUITY_SLIPPAGE_BPS`). Live `execute_order` does not apply that haircut. The daily backtester fills at the same bar’s close. |

The paper overlap flag `PAPER_NYSE_OVERLAP_FILTER_ENABLED` is true after the enforcer. Calling `effective_nyse_overlap_filter_enabled()` in this tree raises `ModuleNotFoundError: modules.core_allocator` (`get_paper_feature_flags` imports it). `modules/core_allocator.py` is not in the repository. `_equity_momentum_ranked` calls that helper with no guard when the SPY sleeve is active, so the anti-overlap branch cannot be evaluated here. This screen did not patch that.

The candidate score, had the data existed, would have been prior-session close about 7 months earlier divided by prior-session close about 12 months earlier, minus 1. Every other eligibility and execution rule would have stayed as in the code above. Parameters were not tuned.

## Data on disk

The only price panel is `stock-bot/data/cache/backtest/days_266.pkl`.

| Item | Coverage |
|------|----------|
| Shape | 426 rows × 62 columns, close only |
| Dates | 2025-04-14 through 2026-06-14 |
| Calendar | 305 weekdays, 121 weekend rows. Every AAPL Saturday equals the prior row (forward-filled calendar daily, not session bars). |
| Names | 22 crypto, sleeve ETFs/metals, 29 equities from the static universe. `SPCX` has 2 non-null closes. `ONDS`, `SUI-USD`, and `PEPE-USD` are in `config.UNIVERSE` and absent here. |
| After a 12-month lag | 61 rows, 2026-04-14 through 2026-06-14 |

Also absent:

- `market_data.db` (the loader’s source for 5-minute and `*_daily` tables)
- Point-in-time S&P 500 membership
- Delisted-name history
- Open/high/low (needed for the gap filter as written, and for a high-low ATR)
- 5-minute bars and `data/intraday_cache/`
- `scripts/research/backtest_intraday.py` (named in the README, not in the tree)

5-minute coverage of point-in-time member observations is **0%**, under the 90% bar. The available 62-name close cache cannot form prior-close 12-to-7 signals on an S&P membership panel, overnight gaps, or next-executable 5-minute fills. No exploratory return is reported, because that subset still cannot implement the required fill and membership rules, and fabricating bars was out of scope.

A valid later run needs, for the same dates and names: membership with as-of dates, delisted names where a history file exists, session opens and closes (and highs/lows if true range is required), and 5-minute bars covering at least 90% of member observations in each test window. History has to leave room for a 12-month lag, purged walk-forward folds, and an untouched final holdout. Until that panel is local, the comparison stays inconclusive.

Re-run the inventory with:

```bash
cd stock-bot
python scripts/research/nyse_12_7_momentum_coverage.py
```

Exit code 2 means the data check still stops before any portfolio simulation.
