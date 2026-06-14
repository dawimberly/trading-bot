# A/B Test Results — Live Stack Backtester (P0)

**Date:** 2026-06-02  
**Executor:** `SleeveAwareBacktestExecutor` (`BacktestExecutor` in `backtester.py`) — equity-based sizing, per-sleeve caps, mirrors `AlpacaExecutor` (`compute_*_notional`, `sleeve_snapshot`, `execute_reduce_notional`, `execute_full_exit`).

## Verification summary

| Check | Result |
|-------|--------|
| Crypto signals > 0 (sleeve-aware executor) | **Yes** — 519 with `python backtester.py --max --no-halt`; 72 in ad-hoc full run without halt |
| Crypto with default 10% halt (`--max`) | **0** — halt on 2016-01-13 stops pipeline before first high-vol crypto window |
| Sleeve entry caps (new buys) | **OK** — no buy exceeded room at entry (`sleeve_snapshot` before orders) |
| MTM sleeve vs cap | Can exceed cap after equity drops (mark-to-market drift); reported separately |
| `macro_stress` / TLT | Uses `window_full` in `backtester_metals.py`, `backtester_wisdom.py`, `backtester_macro_hedge.py` |
| Game plan live script | Fixed `sys.path`; runs without import error |

## Fund backtest (`backtester.py`)

| Run | Return | Sharpe | Max DD | SPY sig | Crypto sig | NYSE sig | Orders | Notes |
|-----|--------|--------|--------|---------|------------|----------|--------|-------|
| `--max` (halt ON) | +730.22% | 0.62 | -58.58% | 30 | **0** | 14 | 44 | Halt 2016-01-13 |
| `--max --no-halt` | +1361.23% | 0.64 | -84.64% | 433 | **519** | 21 | 973 | Validates crypto + caps |
| `--days 730` (halt ON) | +14.64% | 0.59 | -17.38% | 23 | **0** | 23 | 46 | Halt 2025-03-10 |

VTI buy & hold (`--max`): +316.46%.

**Sleeve util (`--max --no-halt`):** SPY 125% / crypto 415% / NYSE 397% MTM vs cap (drift); **entry caps: OK**.

## Metal / game plan (`backtester_metals.py --from 2017 --to 2023`)

| Strategy | Return | Sharpe | Max DD | Metal $ | Gate days | Cash trims |
|----------|--------|--------|--------|---------|-----------|------------|
| baseline | +157.64% | 0.70 | -37.70% | 0 | — | — |
| game_plan_gld_slv_cper (LIVE blend) | +146.94% | 0.71 | -36.19% | 1,490 | 197 | 0 |
| game_plan_gld | +147.34% | 0.71 | -35.86% | 1,530 | 197 | 0 |
| game_plan_basket | +147.56% | 0.70 | -36.32% | 1,552 | 197 | 0 |

Best Sharpe in grid: `gld_only` (0.71). Best return: baseline (+157.64%).

## Live game plan script (`scripts/research/backtest_game_plan_live.py`)

**Full window (2017-07-20 → 2023-12-31)**

| Strategy | Return | Sharpe | Max DD | Metal $ |
|----------|--------|--------|--------|---------|
| baseline | +157.64% | 0.70 | -37.70% | 0 |
| game_plan_gld_slv_cper | +146.94% | 0.71 | -36.19% | 1,490 |

vs baseline: -10.70 pp return, +0.01 Sharpe, +1.51 pp Max DD (less negative).

**Fresh $10k @ 2022-01-01**

| Strategy | Return | Sharpe | Max DD | Metal $ |
|----------|--------|--------|--------|---------|
| baseline | -11.11% | -0.50 | -16.37% | 0 |
| game_plan_gld_slv_cper | -7.01% | -0.45 | -12.22% | 977 |

vs baseline: **+4.10 pp** return, +4.15 pp Max DD improvement; metal sleeve ~$977 (4 trades).

Outputs: `fund_game_plan_live_backtest.csv`, `fund_game_plan_fresh_2022.csv`, `fund_metals_backtest_results.csv`.

## How to run

```bash
# Fund pipeline (daily, mirrors run_all sleeves)
python backtester.py --max
python backtester.py --max --no-halt    # crypto/sleeve validation over full history

# Metal + game plan grid
python backtester_metals.py --from 2017 --to 2023

# Live game plan comparison (full + fresh 2022)
python scripts/research/backtest_game_plan_live.py

# Live stack + wisdom (optional)
python scripts/research/backtest_live_stack.py
python scripts/research/backtest_live_stack.py --from 2022 --to 2022 --fresh
```

## Files changed (P0)

- `backtester.py` — `SleeveAwareBacktestExecutor`: `execute_reduce_notional`, `execute_full_exit`, `_find_position`; sleeve cap reporting; `--no-halt`
- `backtester_macro_hedge.py` — `_sh_exit(..., window_full)` for TLT/SPY macro columns
- `scripts/research/backtest_game_plan_live.py` — repo `sys.path` + CSV paths under project root

## Notes

1. **Crypto = 0 with default halt** is expected when `MAX_DRAWDOWN_PCT` (10%) trips before the first vol-gated crypto entry; use `--no-halt` to confirm the sleeve-aware crypto path.
2. **Daily bars** use the same 0.02 vol threshold as 5m live logic; most regimes read as `RHYME_D` on daily data (see `sleeve_overlap_analysis.py` limitations).
3. **Cash trims = 0** in 2017–2023 game plan runs: stress trim uses `_trim_to_cash_target` on the long book; low cash pressure in that window.
