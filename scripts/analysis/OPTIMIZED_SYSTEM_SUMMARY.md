# Optimized System Summary

**Date:** 2026-06-02  
**Status:** Recommended live + backtest stack after session A/B grids.

## Executive summary

The integrated fund (`run_all.py`) runs three strategy sleeves on one Alpaca paper account with shared risk controls. Post-optimization, the stack favors **macro yield gate only** (no metal sleeve, no stress-cash trim, no 0.9 long-scale haircut), **full sleeve caps** (45% / 20% / 20% / 15% cash), **adaptive deployment sizing**, and **halt resume with breach liquidation**.

## Sleeve allocation (yield-gate-only)

| Sleeve | Cap | Strategy | Session |
|--------|-----|----------|---------|
| SPY | 45% | Price > MA200; exit on MA break | US equity |
| Crypto | 20% | Z-score pairs; **high vol only** | 24/7 |
| NYSE | 20% | Top momentum above MA50 (excludes SPY) | US equity |
| Cash buffer | 15% | Structural headroom | — |
| Metal | 0% | Disabled in yield-gate-only mode | — |

Caps are enforced per buy in `modules/alpaca_executor.py`. `config.fund_allocation_pct()` reports effective fractions at runtime.

## Game plan (yield-gate-only)

| Component | Live default | Notes |
|-----------|--------------|-------|
| `GAME_PLAN_YIELD_GATE_ONLY` | `true` | Blocks new SPY buys when 10Y (TNX) above MA50 and rising; TLT fallback |
| `YIELD_GATE_ENABLED` | `true` | Same gate in backtests |
| Metal sleeve | off | `metal_sleeve_enabled()` false — no GLD/SLV/CPER deploy |
| Stress cash | off | No trim-to-25% on macro stress |
| Long scale 0.9 | off | SPY/crypto/NYSE use **full** base caps |

**A/B (`game_plan_ab_test.py`):** `yield_gate_only` matched baseline return on full 2017–2026 window (~−1.35 pp) with best average Sharpe across windows vs full `game_plan_gld_slv_cper`. Full metal plan cost ~110 pp return on the long window.

Set `GAME_PLAN_YIELD_GATE_ONLY=false` and `GAME_PLAN_ENABLED=true` to restore full game plan (metal + stress cash + 0.9 scale).

## Risk controls

| Control | Default | Source |
|---------|---------|--------|
| Per-order risk | 2% of equity | `RISK_PER_TRADE` |
| Max per order | $10,000 | `MAX_NOTIONAL_PER_ORDER` |
| Stop-loss | 5% | `STOP_LOSS_PCT` |
| Max drawdown halt | 10% | `MAX_DRAWDOWN_PCT` |
| Halt resume | 8% DD | `HALT_RESUME_DRAWDOWN_PCT` |
| Liquidate on breach | true → 25% cash | `HALT_LIQUIDATE_ON_BREACH`, `HALT_TARGET_CASH_PCT` |
| Regime skip | Panic + Steady Bear | `market_context.py` |
| Crypto vol gate | High vol only | `CRYPTO_VOL_ONLY` |
| Crypto pair corr | ≥ 0.5 | `CRYPTO_MIN_CORRELATION` |
| Derived bear pause | **off** | `DERIVED_BEAR_PAUSE_ENABLED=false` |

**A/B (`risk_layer_ab.py`):** Halt resume + liquidate improved 500-day Sharpe (1.24 → 1.77) and max DD (~6 pp). Derived bear pause blocked too many days on daily bars — leave off.

## Deployment sizing

| Flag | Default | Effect |
|------|---------|--------|
| `ADAPTIVE_CHUNK_ENABLED` | `true` | Larger chunks when sleeve room > 5× base risk |
| `ADAPTIVE_CHUNK_MAX_PCT` | `0.05` | Cap per adaptive chunk |
| `COFIRE_BUDGET_ENABLED` | `true` | Shared budget when SPY + NYSE fire same bar |
| `COFIRE_BUDGET_PCT` | `0.06` | Co-fire notional cap |

**A/B (`deployment_efficiency_ab.py`):** Adaptive-only best on 2000d (+145 pp return vs fixed 2% chunks). Both flags on by default in `config.py`.

## SPY / NYSE refinements

| Flag | Default | A/B note |
|------|---------|----------|
| `SPY_EXIT_ON_MA_BREAK` | `true` | Keeps SPY from riding through MA200 breaks |
| `SPY_LADDER_SIZING_ENABLED` | `false` | Ladder helped 2000d Sharpe but hurt 500d — not default |
| `NYSE_OVERLAP_FILTER_ENABLED` | `true` | Skip NYSE pick when corr to SPY > 0.80 (config default) |
| `NYSE_SPY_CORR_MAX` | `0.80` | Grid found corr filter costly on recent_750d — optional off |
| `NYSE_BETA_SCALING_ENABLED` | **`true`** | Best cross-window single toggle in refinements grid (500d + 2000d) |

## Wisdom & sentiment

- `WISDOM_MODE=arbitrage` (default)
- `SENTIMENT_SOURCE=price` (free, matches backtests)
- `REGIME_SENTIMENT_THRESHOLD=0.5` (legacy; derived bear off)

## Backtest parity

| Script | Purpose |
|--------|---------|
| `backtester.py` | Integrated fund; `--max`, `--no-halt`, `--days N` |
| `backtester_metals.py` | Game plan variants incl. `yield_gate_only` |
| `backtester_macro_hedge.py` | Yield gate / GLD / stress cash grid |
| `backtester_wisdom.py` | Wisdom modes + game plan |
| `scripts/research/backtest_game_plan_live.py` | Live blend vs baseline CSVs |

Startup prints `config.print_recommended_stack_flags()` from preflight and backtesters.

## Analysis artifacts (repo)

| Report | Topic |
|--------|-------|
| `game_plan_ab_results.md` | Yield-gate-only vs full game plan |
| `risk_layer_results.md` | Halt resume + liquidate |
| `deployment_efficiency_results.md` | Adaptive chunk / co-fire |
| `refinements_grid_results.md` | SPY exit, ladder, NYSE beta |
| `nyse_anti_overlap_results.md` | Corr filter vs baseline |
| `ab_test_results_live_stack.md` | Sleeve-aware executor P0 |

Re-run grids: `python scripts/analysis/game_plan_ab_test.py`, `risk_layer_ab.py`, `deployment_efficiency_ab.py`, `refinements_grid_ab.py`, `nyse_anti_overlap_ab.py`.

## Recommended `.env` (minimal)

```env
GAME_PLAN_ENABLED=true
GAME_PLAN_YIELD_GATE_ONLY=true
YIELD_GATE_ENABLED=true
ADAPTIVE_CHUNK_ENABLED=true
COFIRE_BUDGET_ENABLED=true
SPY_EXIT_ON_MA_BREAK=true
HALT_RESUME_DRAWDOWN_PCT=0.08
HALT_LIQUIDATE_ON_BREACH=true
DERIVED_BEAR_PAUSE_ENABLED=false
NYSE_BETA_SCALING_ENABLED=true
WISDOM_MODE=arbitrage
SENTIMENT_SOURCE=price
```

See `.env.example` for the full commented list.
